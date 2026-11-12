//! Strict, bounded JSON-RPC 2.0 model for the native relay.

use std::collections::BTreeMap;
use std::net::IpAddr;

use base64::{engine::general_purpose::STANDARD, Engine};
use sha2::{Digest, Sha256};

use crate::json::{self, Value};

pub const BRIDGE_PROTOCOL: &str = "a0.browser-bridge";
pub const BRIDGE_PROTOCOL_VERSIONED: &str = "a0.browser-bridge.v1";
pub const CONTRACT_VERSION: u64 = 1;
pub const MAX_NATIVE_FRAME_BYTES: usize = 768 * 1024;
pub const MAX_NON_ARTIFACT_BYTES: usize = 512 * 1024;
pub const MAX_ARTIFACT_CHUNK_RAW_BYTES: usize = 192 * 1024;
pub const MAX_ARTIFACT_CHUNK_BASE64_BYTES: usize = 256 * 1024;
pub const MAX_ARTIFACT_BYTES: u64 = 25 * 1024 * 1024;
pub const MAX_DIAGNOSTIC_BYTES: usize = 2_048;
pub const MAX_EVENT_METADATA_BYTES: usize = 64 * 1024;
pub const MAX_REQUEST_TIMEOUT_MS: u64 = 120_000;
pub const HELLO_TIMEOUT_MS: u64 = 10_000;
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

pub const EXTENSION_REQUEST_METHODS: &[&str] = &[
    "artifact.input_path",
    "bridge.hello",
    "bridge.ping",
    "pairing.status",
    "pairing.exchange",
    "pairing.disconnect",
    "credential.rotate",
    "credential.status",
    "credential.revoke",
    "agent.status",
    "context.list",
    "context.subscribe",
    "context.unsubscribe",
    "context.send_message",
    "context.queue_add",
    "context.queue_remove",
    "context.queue_send",
    "browser.approval_decision",
    "artifact.begin",
    "artifact.chunk",
    "artifact.end",
    "artifact.abort",
];

pub const EXTENSION_NOTIFICATION_METHODS: &[&str] = &["browser.event"];

pub const SERVER_REQUEST_METHODS: &[&str] = &[
    "bridge.ping",
    "browser.perform",
    "browser.cancel",
    "browser.finalize_turn",
    "browser.resolve_challenge",
    "browser.reconcile",
    "browser.ack_events",
    "artifact.begin",
    "artifact.chunk",
    "artifact.end",
    "artifact.abort",
];

pub const SERVER_NOTIFICATION_METHODS: &[&str] = &[
    "context.snapshot",
    "context.event",
    "context.complete",
    "context.queue_updated",
    "credential.changed",
];

pub const BROWSER_ACTIONS: &[&str] = &[
    "open",
    "list",
    "state",
    "set_active",
    "navigate",
    "back",
    "forward",
    "reload",
    "content",
    "detail",
    "evaluate",
    "click",
    "type",
    "submit",
    "type_submit",
    "scroll",
    "hover",
    "double_click",
    "right_click",
    "drag",
    "wheel",
    "mouse",
    "keyboard",
    "key_chord",
    "clipboard",
    "set_viewport",
    "select_option",
    "set_checked",
    "upload_file",
    "screenshot",
    "close",
    "close_all",
    "multi",
    "ensure",
    "status",
    "claim",
];

pub const BROWSER_EVENT_TYPES: &[&str] = &[
    "lease.changed",
    "challenge.required",
    "turn.finalized",
    "operation.outcome_unknown",
    "activity.changed",
    "cursor.arrived",
    "tab.changed",
    "diagnostic",
];

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Peer {
    Extension,
    Server,
}

impl Peer {
    pub const fn opposite(self) -> Self {
        match self {
            Self::Extension => Self::Server,
            Self::Server => Self::Extension,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RpcRequest {
    pub id: Option<String>,
    pub method: String,
    pub params: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RpcErrorObject {
    pub code: i64,
    pub message: String,
    pub data: Option<Value>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RpcResponse {
    pub id: String,
    pub result: Result<Value, RpcErrorObject>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum RpcMessage {
    Request(RpcRequest),
    Response(RpcResponse),
}

impl RpcMessage {
    pub fn encode(&self) -> Vec<u8> {
        let value = match self {
            Self::Request(request) => {
                let mut fields = BTreeMap::new();
                fields.insert("jsonrpc".to_owned(), Value::String("2.0".to_owned()));
                if let Some(id) = &request.id {
                    fields.insert("id".to_owned(), Value::String(id.clone()));
                }
                fields.insert("method".to_owned(), Value::String(request.method.clone()));
                fields.insert("params".to_owned(), request.params.clone());
                Value::Object(fields)
            }
            Self::Response(response) => {
                let mut fields = BTreeMap::new();
                fields.insert("jsonrpc".to_owned(), Value::String("2.0".to_owned()));
                fields.insert("id".to_owned(), Value::String(response.id.clone()));
                match &response.result {
                    Ok(result) => {
                        fields.insert("result".to_owned(), result.clone());
                    }
                    Err(error) => {
                        let mut error_fields = BTreeMap::new();
                        error_fields
                            .insert("code".to_owned(), Value::Number(error.code.to_string()));
                        error_fields
                            .insert("message".to_owned(), Value::String(error.message.clone()));
                        if let Some(data) = &error.data {
                            error_fields.insert("data".to_owned(), data.clone());
                        }
                        fields.insert("error".to_owned(), Value::Object(error_fields));
                    }
                }
                Value::Object(fields)
            }
        };
        value.encode().into_bytes()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RpcValidationError {
    FrameTooLarge,
    NonArtifactPayloadTooLarge,
    JsonInvalid,
    BatchForbidden,
    EnvelopeInvalid,
    IdentifierInvalid,
    MethodNotAllowed,
    RequestKindInvalid,
    ParamsInvalid,
    VersionMismatch,
    ExtensionIdentityMismatch,
    DeadlineInvalid,
    ArtifactLimitInvalid,
}

impl RpcValidationError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::FrameTooLarge => "NATIVE_FRAME_TOO_LARGE",
            Self::NonArtifactPayloadTooLarge => "NATIVE_PAYLOAD_TOO_LARGE",
            Self::JsonInvalid => "NATIVE_JSON_INVALID",
            Self::BatchForbidden => "NATIVE_JSONRPC_BATCH_FORBIDDEN",
            Self::EnvelopeInvalid => "NATIVE_JSONRPC_ENVELOPE_INVALID",
            Self::IdentifierInvalid => "NATIVE_JSONRPC_ID_INVALID",
            Self::MethodNotAllowed => "NATIVE_JSONRPC_METHOD_NOT_ALLOWED",
            Self::RequestKindInvalid => "NATIVE_JSONRPC_REQUEST_KIND_INVALID",
            Self::ParamsInvalid => "NATIVE_JSONRPC_PARAMS_INVALID",
            Self::VersionMismatch => "VERSION_MISMATCH",
            Self::ExtensionIdentityMismatch => "EXTENSION_IDENTITY_MISMATCH",
            Self::DeadlineInvalid => "DEADLINE_EXCEEDED",
            Self::ArtifactLimitInvalid => "ARTIFACT_TOO_LARGE",
        }
    }
}

pub fn parse_message(payload: &[u8], source: Peer) -> Result<RpcMessage, RpcValidationError> {
    if payload.len() > MAX_NATIVE_FRAME_BYTES {
        return Err(RpcValidationError::FrameTooLarge);
    }
    let value = json::parse(payload).map_err(|_| RpcValidationError::JsonInvalid)?;
    if matches!(value, Value::Array(_)) {
        return Err(RpcValidationError::BatchForbidden);
    }
    let fields = value
        .as_object()
        .ok_or(RpcValidationError::EnvelopeInvalid)?;
    require_exact_string(fields, "jsonrpc", "2.0")?;

    let message = if fields.contains_key("method") {
        parse_request(fields, source)?
    } else {
        parse_response(fields)?
    };
    let artifact = matches!(
        &message,
        RpcMessage::Request(request) if request.method.starts_with("artifact.")
    );
    if !artifact && payload.len() > MAX_NON_ARTIFACT_BYTES {
        return Err(RpcValidationError::NonArtifactPayloadTooLarge);
    }
    Ok(message)
}

fn parse_request(
    fields: &BTreeMap<String, Value>,
    source: Peer,
) -> Result<RpcMessage, RpcValidationError> {
    if fields.contains_key("result") || fields.contains_key("error") {
        return Err(RpcValidationError::EnvelopeInvalid);
    }
    let method = required_string(fields, "method", 128)?.to_owned();
    let params = fields
        .get("params")
        .filter(|value| value.as_object().is_some())
        .ok_or(RpcValidationError::ParamsInvalid)?
        .clone();
    let id = fields
        .get("id")
        .map(|value| {
            value
                .as_str()
                .filter(|id| bounded_rpc_id(id))
                .map(str::to_owned)
                .ok_or(RpcValidationError::IdentifierInvalid)
        })
        .transpose()?;
    validate_method(source, &method, id.is_some())?;
    validate_params(&method, &params)?;
    Ok(RpcMessage::Request(RpcRequest { id, method, params }))
}

fn parse_response(fields: &BTreeMap<String, Value>) -> Result<RpcMessage, RpcValidationError> {
    if fields.contains_key("method")
        || fields.contains_key("params")
        || fields.contains_key("result") == fields.contains_key("error")
    {
        return Err(RpcValidationError::EnvelopeInvalid);
    }
    let id = fields
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| bounded_rpc_id(id))
        .ok_or(RpcValidationError::IdentifierInvalid)?;
    let result = if let Some(result) = fields.get("result") {
        Ok(result.clone())
    } else {
        Err(parse_error_object(
            fields
                .get("error")
                .ok_or(RpcValidationError::EnvelopeInvalid)?,
        )?)
    };
    Ok(RpcMessage::Response(RpcResponse {
        id: id.to_owned(),
        result,
    }))
}

fn parse_error_object(value: &Value) -> Result<RpcErrorObject, RpcValidationError> {
    let fields = value
        .as_object()
        .ok_or(RpcValidationError::EnvelopeInvalid)?;
    if fields
        .keys()
        .any(|key| !matches!(key.as_str(), "code" | "message" | "data"))
    {
        return Err(RpcValidationError::EnvelopeInvalid);
    }
    let code = fields
        .get("code")
        .and_then(|value| match value {
            Value::Number(number)
                if !number.contains('.') && !number.contains('e') && !number.contains('E') =>
            {
                number.parse::<i64>().ok()
            }
            _ => None,
        })
        .ok_or(RpcValidationError::EnvelopeInvalid)?;
    let message = required_string(fields, "message", MAX_DIAGNOSTIC_BYTES)?.to_owned();
    let data = fields.get("data").cloned();
    if data
        .as_ref()
        .is_some_and(|value| value.as_object().is_none())
    {
        return Err(RpcValidationError::EnvelopeInvalid);
    }
    Ok(RpcErrorObject {
        code,
        message,
        data,
    })
}

fn validate_method(source: Peer, method: &str, request: bool) -> Result<(), RpcValidationError> {
    let allowed = match (source, request) {
        (Peer::Extension, true) => EXTENSION_REQUEST_METHODS,
        (Peer::Extension, false) => EXTENSION_NOTIFICATION_METHODS,
        (Peer::Server, true) => SERVER_REQUEST_METHODS,
        (Peer::Server, false) => SERVER_NOTIFICATION_METHODS,
    };
    if !allowed.contains(&method) {
        let known_other_kind = match source {
            Peer::Extension => EXTENSION_REQUEST_METHODS
                .iter()
                .chain(EXTENSION_NOTIFICATION_METHODS)
                .any(|candidate| *candidate == method),
            Peer::Server => SERVER_REQUEST_METHODS
                .iter()
                .chain(SERVER_NOTIFICATION_METHODS)
                .any(|candidate| *candidate == method),
        };
        return Err(if known_other_kind {
            RpcValidationError::RequestKindInvalid
        } else {
            RpcValidationError::MethodNotAllowed
        });
    }
    Ok(())
}

fn validate_params(method: &str, params: &Value) -> Result<(), RpcValidationError> {
    let fields = params
        .as_object()
        .ok_or(RpcValidationError::ParamsInvalid)?;
    match method {
        "bridge.hello" => validate_hello(fields),
        "bridge.ping" => validate_ping(fields),
        "browser.perform" => validate_browser_perform(fields),
        "browser.cancel" => validate_browser_cancel(fields),
        "browser.finalize_turn" => validate_browser_finalize(fields),
        "browser.resolve_challenge" => validate_browser_resolve_challenge(fields),
        "browser.reconcile" => validate_browser_reconcile(fields),
        "browser.ack_events" => validate_event_ack(fields),
        "browser.event" => validate_browser_event(fields),
        "artifact.begin" => validate_artifact_begin(fields),
        "artifact.chunk" => validate_artifact_chunk(fields),
        "artifact.end" => validate_artifact_end(fields),
        "artifact.abort" => validate_artifact_abort(fields),
        "artifact.input_path" => {
            const KEYS: &[&str] = &[
                "contract_version",
                "context_id",
                "browser_session_id",
                "turn_id",
                "action_id",
                "op_id",
                "artifact_id",
                "direction",
                "purpose",
            ];
            if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
                return Err(RpcValidationError::ParamsInvalid);
            }
            require_contract(fields)?;
            for key in &KEYS[1..7] {
                required_opaque_id(fields, key)?;
            }
            if required_bounded_string(fields, "direction", 16)? != "input"
                || required_bounded_string(fields, "purpose", 32)? != "upload_file"
            {
                return Err(RpcValidationError::ParamsInvalid);
            }
            Ok(())
        }
        "pairing.exchange" => {
            if fields.len() != 3
                || fields.keys().any(|key| {
                    !matches!(
                        key.as_str(),
                        "contract_version" | "pairing_code" | "server_base_origin"
                    )
                })
            {
                return Err(RpcValidationError::ParamsInvalid);
            }
            require_contract(fields)?;
            let pairing_code = required_bounded_string(fields, "pairing_code", 256)?;
            if !valid_pairing_code(pairing_code) {
                return Err(RpcValidationError::ParamsInvalid);
            }
            let server_base_origin = required_bounded_string(fields, "server_base_origin", 2_048)?;
            if !valid_pairing_server_base_origin(server_base_origin) {
                return Err(RpcValidationError::ParamsInvalid);
            }
            Ok(())
        }
        _ => {
            require_contract(fields)?;
            validate_bounded_tree(params, 0)
        }
    }
}

fn validate_hello(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_exact_string(fields, "protocol", BRIDGE_PROTOCOL)?;
    let contract = required_object(fields, "contract")?;
    let minimum = required_u64(contract, "min")?;
    let maximum = required_u64(contract, "max")?;
    if minimum > CONTRACT_VERSION || maximum < CONTRACT_VERSION || minimum > maximum {
        return Err(RpcValidationError::VersionMismatch);
    }
    let extension = required_object(fields, "extension")?;
    let extension_id = required_bounded_string(extension, "id", 32)?;
    if !is_extension_id(extension_id) {
        return Err(RpcValidationError::ExtensionIdentityMismatch);
    }
    required_bounded_string(extension, "version", 64)?;
    if required_u64(extension, "manifest_version")? != 3 {
        return Err(RpcValidationError::ParamsInvalid);
    }
    if !valid_opaque_id(required_bounded_string(
        extension,
        "install_instance_id",
        256,
    )?) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_bounded_string(extension, "load_generation_id", 256)?;

    let browser = required_object(fields, "browser")?;
    required_bounded_string(browser, "family", 64)?;
    required_bounded_string(browser, "version", 64)?;

    let capabilities = required_object(fields, "capabilities")?;
    validate_string_array(capabilities, "actions", 64, 128, Some(BROWSER_ACTIONS))?;
    validate_string_array(capabilities, "features", 64, 128, None)?;
    validate_string_array(capabilities, "cdp_domains", 32, 128, None)?;

    let resume = required_object(fields, "resume")?;
    let cursors = required_array(resume, "event_cursors")?;
    if cursors.len() > 8 {
        return Err(RpcValidationError::ParamsInvalid);
    }
    for cursor in cursors {
        let cursor = cursor
            .as_object()
            .ok_or(RpcValidationError::ParamsInvalid)?;
        required_bounded_string(cursor, "load_generation_id", 256)?;
        required_u64(cursor, "last_acked_event_sequence")?;
    }
    validate_string_array(resume, "inflight_op_ids", 256, 256, None)?;
    if !valid_sha256_digest(required_bounded_string(resume, "lease_digest", 71)?) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    validate_bounded_tree(&Value::Object(fields.clone()), 0)
}

fn validate_ping(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    if fields.keys().any(|key| key != "nonce") {
        return Err(RpcValidationError::ParamsInvalid);
    }
    if let Some(nonce) = fields.get("nonce") {
        if !nonce
            .as_str()
            .is_some_and(|value| bounded_identifier(value, 128))
        {
            return Err(RpcValidationError::ParamsInvalid);
        }
    }
    Ok(())
}

fn validate_browser_perform(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_contract(fields)?;
    let action = required_bounded_string(fields, "action", 64)?;
    if !BROWSER_ACTIONS.contains(&action) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    for key in [
        "op_id",
        "action_id",
        "context_id",
        "browser_session_id",
        "turn_id",
    ] {
        if matches!(action, "status" | "ensure") {
            if let Some(value) = fields.get(key) {
                if !value
                    .as_str()
                    .is_some_and(|value| bounded_identifier(value, 256))
                {
                    return Err(RpcValidationError::ParamsInvalid);
                }
            }
        } else {
            required_bounded_string(fields, key, 256)?;
        }
    }
    fields
        .get("args")
        .filter(|value| value.as_object().is_some())
        .ok_or(RpcValidationError::ParamsInvalid)?;
    if let Some(target) = fields.get("target") {
        if !matches!(target, Value::Null) {
            required_bounded_string(
                target
                    .as_object()
                    .ok_or(RpcValidationError::ParamsInvalid)?,
                "tab_handle",
                256,
            )?;
        }
    }
    let timeout = required_u64(fields, "timeout_ms")?;
    if timeout == 0 || timeout > MAX_REQUEST_TIMEOUT_MS {
        return Err(RpcValidationError::DeadlineInvalid);
    }
    validate_string_array(fields, "required_capabilities", 64, 128, None)?;
    required_object(fields, "policy")?;
    required_object(fields, "display")?;
    match action {
        "click" => validate_click_perform(fields)?,
        "type" => validate_type_perform(fields)?,
        "upload_file" => validate_upload_perform(fields)?,
        _ => {}
    }
    validate_bounded_tree(&Value::Object(fields.clone()), 0)
}

fn validate_upload_perform(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    let args = required_object(fields, "args")?;
    let keys = [
        "ref",
        "expected_action_class",
        "artifact_id",
        "mime_type",
        "byte_count",
        "sha256",
    ];
    if args.len() != keys.len()
        || keys.iter().any(|key| !args.contains_key(*key))
        || required_bounded_string(args, "expected_action_class", 32)? != "external_side_effect"
        || !valid_mime_type(required_bounded_string(args, "mime_type", 255)?)
        || !valid_sha256_digest(required_bounded_string(args, "sha256", 71)?)
        || !(1..=26_214_400).contains(&required_u64(args, "byte_count")?)
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(args, "artifact_id")?;
    let capabilities = required_array(fields, "required_capabilities")?;
    if [
        "upload_file",
        "artifacts_v1",
        "trusted_input_v1",
        "semantic_dom_v1",
    ]
    .iter()
    .any(|required| {
        !capabilities
            .iter()
            .any(|value| value.as_str() == Some(required))
    }) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    // Share exact envelope/target/policy constraints with semantic click; the
    // upload-specific metadata is already fully validated above.
    let mut semantic = fields.clone();
    semantic.insert(
        "args".into(),
        Value::Object(BTreeMap::from([
            ("ref".into(), args["ref"].clone()),
            (
                "expected_action_class".into(),
                args["expected_action_class"].clone(),
            ),
        ])),
    );
    let mut capabilities = capabilities.to_vec();
    capabilities.push(Value::String("click".into()));
    semantic.insert("required_capabilities".into(), Value::Array(capabilities));
    validate_click_perform(&semantic)
}

fn validate_click_perform(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    const KEYS: &[&str] = &[
        "contract_version",
        "op_id",
        "action_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "action",
        "target",
        "args",
        "timeout_ms",
        "required_capabilities",
        "policy",
        "display",
    ];
    if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let target = required_object(fields, "target")?;
    if target.len() != 1 || !target.contains_key("tab_handle") {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(target, "tab_handle")?;

    let args = required_object(fields, "args")?;
    if args.len() != 2 || !args.contains_key("ref") || !args.contains_key("expected_action_class") {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let reference = required_bounded_string(args, "ref", 128)?;
    if !valid_opaque_id(reference)
        || !matches!(
            required_bounded_string(args, "expected_action_class", 32)?,
            "reversible_input" | "sensitive_input" | "external_side_effect" | "unknown"
        )
    {
        return Err(RpcValidationError::ParamsInvalid);
    }

    let policy = required_object(fields, "policy")?;
    if policy.len() != 2
        || !policy.contains_key("origin_grant_id")
        || !policy.contains_key("action_grant_id")
        || !matches!(policy.get("action_grant_id"), Some(Value::Null))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(policy, "origin_grant_id")?;

    let display = required_object(fields, "display")?;
    if display.len() != 2
        || !matches!(display.get("cursor"), Some(Value::Bool(_)))
        || !matches!(display.get("foreground"), Some(Value::Bool(_)))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let capabilities = required_array(fields, "required_capabilities")?;
    if !capabilities
        .iter()
        .any(|value| value.as_str() == Some("click"))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_type_perform(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    const KEYS: &[&str] = &[
        "contract_version",
        "op_id",
        "action_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "action",
        "target",
        "args",
        "timeout_ms",
        "required_capabilities",
        "policy",
        "display",
    ];
    if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let target = required_object(fields, "target")?;
    if target.len() != 1 || !target.contains_key("tab_handle") {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(target, "tab_handle")?;

    let args = required_object(fields, "args")?;
    const ARG_KEYS: &[&str] = &["ref", "text", "text_sha256", "expected_action_class"];
    if args.len() != ARG_KEYS.len() || ARG_KEYS.iter().any(|key| !args.contains_key(*key)) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let reference = required_bounded_string(args, "ref", 128)?;
    let text = required_string(args, "text", 32_768)?;
    let text_sha256 = required_bounded_string(args, "text_sha256", 64)?;
    if !valid_opaque_id(reference)
        || text.is_empty()
        || text.contains('\0')
        || text.contains('\r')
        || args.get("expected_action_class").and_then(Value::as_str) != Some("sensitive_input")
        || !valid_lower_sha256(text_sha256)
        || lower_hex(&Sha256::digest(text.as_bytes())) != text_sha256
    {
        return Err(RpcValidationError::ParamsInvalid);
    }

    let policy = required_object(fields, "policy")?;
    if policy.len() != 2
        || !policy.contains_key("origin_grant_id")
        || !policy.contains_key("action_grant_id")
        || !matches!(policy.get("action_grant_id"), Some(Value::Null))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(policy, "origin_grant_id")?;

    let display = required_object(fields, "display")?;
    if display.len() != 2
        || !matches!(display.get("cursor"), Some(Value::Bool(_)))
        || !matches!(display.get("foreground"), Some(Value::Bool(_)))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let capabilities = required_array(fields, "required_capabilities")?;
    if !capabilities
        .iter()
        .any(|value| value.as_str() == Some("type"))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_browser_cancel(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_contract(fields)?;
    for key in [
        "control_id",
        "op_id",
        "action_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "reason",
    ] {
        required_bounded_string(fields, key, if key == "reason" { 2_048 } else { 256 })?;
    }
    Ok(())
}

fn validate_browser_finalize(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_contract(fields)?;
    for key in [
        "control_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "reason",
    ] {
        required_bounded_string(fields, key, if key == "reason" { 2_048 } else { 256 })?;
    }
    let dispositions = required_object(fields, "dispositions")?;
    if dispositions.len() > 256
        || dispositions.iter().any(|(lease, disposition)| {
            !bounded_identifier(lease, 256)
                || !disposition
                    .as_str()
                    .is_some_and(|value| matches!(value, "ephemeral" | "deliverable" | "handoff"))
        })
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_browser_resolve_challenge(
    fields: &BTreeMap<String, Value>,
) -> Result<(), RpcValidationError> {
    const SITE_KEYS: &[&str] = &[
        "contract_version",
        "control_id",
        "challenge_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "op_id",
        "action_id",
        "tab_handle",
        "document_id",
        "document_epoch",
        "canonical_parameter_hash",
        "target_fingerprint",
        "origin",
        "action_class",
        "decision",
        "grant",
    ];
    const ACTION_KEYS: &[&str] = &[
        "contract_version",
        "control_id",
        "challenge_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "op_id",
        "action_id",
        "tab_handle",
        "document_id",
        "document_epoch",
        "canonical_parameter_hash",
        "target_fingerprint",
        "origin",
        "action_class",
        "data_classification",
        "decision",
        "grant",
    ];
    let action_class = fields
        .get("action_class")
        .and_then(Value::as_str)
        .ok_or(RpcValidationError::ParamsInvalid)?;
    let expected_keys = if action_class == "navigate" {
        SITE_KEYS
    } else {
        ACTION_KEYS
    };
    if fields.len() != expected_keys.len()
        || expected_keys.iter().any(|key| !fields.contains_key(*key))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    require_contract(fields)?;
    for key in [
        "control_id",
        "challenge_id",
        "context_id",
        "browser_session_id",
        "turn_id",
        "op_id",
        "action_id",
        "tab_handle",
    ] {
        required_opaque_id(fields, key)?;
    }
    let document_id_is_null = matches!(fields.get("document_id"), Some(Value::Null));
    if !document_id_is_null {
        required_opaque_id(fields, "document_id")?;
    }
    if required_u64(fields, "document_epoch")? > MAX_SAFE_INTEGER {
        return Err(RpcValidationError::ParamsInvalid);
    }
    for key in ["canonical_parameter_hash", "target_fingerprint"] {
        if !valid_lower_sha256(required_bounded_string(fields, key, 64)?) {
            return Err(RpcValidationError::ParamsInvalid);
        }
    }
    let origin = required_bounded_string(fields, "origin", 512)?;
    if !valid_http_origin(origin) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let decision = required_bounded_string(fields, "decision", 16)?;
    let grant = fields
        .get("grant")
        .ok_or(RpcValidationError::ParamsInvalid)?;
    if action_class == "navigate" {
        return match decision {
            "deny" if matches!(grant, Value::Null) => Ok(()),
            "allow_once" => validate_site_grant(grant, "operation", origin),
            "allow_turn" => validate_site_grant(grant, "turn", origin),
            _ => Err(RpcValidationError::ParamsInvalid),
        };
    }
    let data_classification = fields
        .get("data_classification")
        .ok_or(RpcValidationError::ParamsInvalid)?;
    if document_id_is_null
        || !matches!(
            action_class,
            "sensitive_input" | "external_side_effect" | "unknown"
        )
        || validate_action_data_classification(data_classification).is_err()
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    match decision {
        "decline" if matches!(grant, Value::Null) => Ok(()),
        "approve_once" => validate_action_grant(
            grant,
            origin,
            action_class,
            required_bounded_string(fields, "canonical_parameter_hash", 64)?,
            required_bounded_string(fields, "target_fingerprint", 64)?,
            data_classification,
        ),
        _ => Err(RpcValidationError::ParamsInvalid),
    }
}

fn validate_site_grant(
    value: &Value,
    expected_scope: &str,
    expected_origin: &str,
) -> Result<(), RpcValidationError> {
    const KEYS: &[&str] = &["origin_grant_id", "scope", "origin", "expires_at_ms"];
    let fields = value.as_object().ok_or(RpcValidationError::ParamsInvalid)?;
    if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(fields, "origin_grant_id")?;
    if required_bounded_string(fields, "scope", 16)? != expected_scope
        || required_bounded_string(fields, "origin", 512)? != expected_origin
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let expires_at_ms = required_u64(fields, "expires_at_ms")?;
    if expires_at_ms == 0 || expires_at_ms > MAX_SAFE_INTEGER {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_action_grant(
    value: &Value,
    expected_origin: &str,
    expected_action_class: &str,
    expected_parameter_hash: &str,
    expected_target_fingerprint: &str,
    expected_data_classification: &Value,
) -> Result<(), RpcValidationError> {
    const KEYS: &[&str] = &[
        "action_grant_id",
        "scope",
        "origin",
        "action_class",
        "canonical_parameter_hash",
        "target_fingerprint",
        "data_classification",
        "expires_at_ms",
    ];
    let fields = value.as_object().ok_or(RpcValidationError::ParamsInvalid)?;
    if fields.len() != KEYS.len() || KEYS.iter().any(|key| !fields.contains_key(*key)) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_opaque_id(fields, "action_grant_id")?;
    if required_bounded_string(fields, "scope", 16)? != "operation"
        || required_bounded_string(fields, "origin", 512)? != expected_origin
        || required_bounded_string(fields, "action_class", 32)? != expected_action_class
        || required_bounded_string(fields, "canonical_parameter_hash", 64)?
            != expected_parameter_hash
        || required_bounded_string(fields, "target_fingerprint", 64)? != expected_target_fingerprint
        || validate_action_data_classification(
            fields
                .get("data_classification")
                .ok_or(RpcValidationError::ParamsInvalid)?,
        )
        .is_err()
        || fields.get("data_classification") != Some(expected_data_classification)
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let expires_at_ms = required_u64(fields, "expires_at_ms")?;
    if expires_at_ms == 0 || expires_at_ms > MAX_SAFE_INTEGER {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_action_data_classification(value: &Value) -> Result<(), RpcValidationError> {
    if value.as_str() == Some("none") {
        return Ok(());
    }
    let fields = value.as_object().ok_or(RpcValidationError::ParamsInvalid)?;
    const KEYS: &[&str] = &["kind", "sensitivity", "text_sha256"];
    if fields.len() != KEYS.len()
        || KEYS.iter().any(|key| !fields.contains_key(*key))
        || fields.get("kind").and_then(Value::as_str) != Some("text")
        || fields.get("sensitivity").and_then(Value::as_str) != Some("sensitive")
        || !fields
            .get("text_sha256")
            .and_then(Value::as_str)
            .is_some_and(valid_lower_sha256)
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_browser_reconcile(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_contract(fields)?;
    required_bounded_string(fields, "control_id", 256)?;
    let contexts = required_array(fields, "expected_contexts")?;
    if contexts.len() > 128 {
        return Err(RpcValidationError::ParamsInvalid);
    }
    for context in contexts {
        let context = context
            .as_object()
            .ok_or(RpcValidationError::ParamsInvalid)?;
        required_bounded_string(context, "context_id", 256)?;
        required_bounded_string(context, "browser_session_id", 256)?;
        validate_string_array(context, "active_turn_ids", 32, 256, None)?;
    }
    let cursors = required_array(fields, "event_cursors")?;
    if cursors.len() > 16 {
        return Err(RpcValidationError::ParamsInvalid);
    }
    for cursor in cursors {
        let cursor = cursor
            .as_object()
            .ok_or(RpcValidationError::ParamsInvalid)?;
        required_bounded_string(cursor, "load_generation_id", 256)?;
        required_u64(cursor, "last_acked_event_sequence")?;
    }
    validate_string_array(fields, "known_control_ids", 2_048, 256, None)?;
    validate_bounded_tree(&Value::Object(fields.clone()), 0)
}

fn validate_browser_event(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_contract(fields)?;
    for key in [
        "event_id",
        "load_generation_id",
        "context_id",
        "browser_session_id",
        "turn_id",
    ] {
        required_bounded_string(fields, key, 256)?;
    }
    if !matches!(
        required_bounded_string(fields, "delivery", 32)?,
        "critical" | "best_effort"
    ) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let event_type = required_bounded_string(fields, "event_type", 64)?;
    if !BROWSER_EVENT_TYPES.contains(&event_type) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    required_u64(fields, "event_sequence")?;
    required_u64(fields, "observed_at_ms")?;
    let data = fields
        .get("data")
        .filter(|value| value.as_object().is_some())
        .ok_or(RpcValidationError::ParamsInvalid)?;
    if data.encode().len() > MAX_EVENT_METADATA_BYTES {
        return Err(RpcValidationError::ParamsInvalid);
    }
    validate_bounded_tree(&Value::Object(fields.clone()), 0)
}

fn validate_event_ack(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    require_contract(fields)?;
    required_bounded_string(fields, "load_generation_id", 256)?;
    required_u64(fields, "highest_contiguous_event_sequence")?;
    Ok(())
}

fn validate_artifact_begin(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    validate_artifact_common(fields, &["mime_type", "byte_count", "sha256"])?;
    let byte_count = required_u64(fields, "byte_count")?;
    if byte_count > MAX_ARTIFACT_BYTES {
        return Err(RpcValidationError::ArtifactLimitInvalid);
    }
    if !required_bounded_string(fields, "sha256", 71)?
        .strip_prefix("sha256:")
        .is_some_and(valid_lower_sha256)
        || !valid_mime_type(required_bounded_string(fields, "mime_type", 255)?)
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_artifact_chunk(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    validate_artifact_common(fields, &["chunk_index", "data"])?;
    if required_u64(fields, "chunk_index")? > MAX_SAFE_INTEGER {
        return Err(RpcValidationError::ParamsInvalid);
    }
    let data = fields
        .get("data")
        .and_then(Value::as_str)
        .ok_or(RpcValidationError::ParamsInvalid)?;
    if data.is_empty() || data.len() > MAX_ARTIFACT_CHUNK_BASE64_BYTES {
        return Err(RpcValidationError::ArtifactLimitInvalid);
    }
    let decoded = STANDARD
        .decode(data)
        .map_err(|_| RpcValidationError::ParamsInvalid)?;
    if decoded.is_empty()
        || decoded.len() > MAX_ARTIFACT_CHUNK_RAW_BYTES
        || STANDARD.encode(&decoded) != data
    {
        return Err(RpcValidationError::ArtifactLimitInvalid);
    }
    Ok(())
}

fn validate_artifact_end(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    validate_artifact_common(fields, &[])
}

fn validate_artifact_abort(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    validate_artifact_common(fields, &["reason_code"])?;
    if !matches!(
        required_bounded_string(fields, "reason_code", 64)?,
        "ARTIFACT_TOO_LARGE"
            | "CANCELED"
            | "CONNECTION_LOST"
            | "DEADLINE_EXCEEDED"
            | "INTERNAL_ERROR"
            | "OUTCOME_UNKNOWN"
    ) {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn validate_artifact_common(
    fields: &BTreeMap<String, Value>,
    additional: &[&str],
) -> Result<(), RpcValidationError> {
    const COMMON: &[&str] = &[
        "contract_version",
        "context_id",
        "browser_session_id",
        "turn_id",
        "action_id",
        "op_id",
        "artifact_id",
        "direction",
        "purpose",
    ];
    if fields.len() != COMMON.len() + additional.len()
        || COMMON
            .iter()
            .chain(additional)
            .any(|key| !fields.contains_key(*key))
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    require_contract(fields)?;
    for key in [
        "context_id",
        "browser_session_id",
        "turn_id",
        "action_id",
        "op_id",
        "artifact_id",
    ] {
        required_opaque_id(fields, key)?;
    }
    if required_bounded_string(fields, "direction", 16)? != "output"
        || !matches!(
            required_bounded_string(fields, "purpose", 32)?,
            "screenshot" | "download"
        )
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn valid_mime_type(value: &str) -> bool {
    value.split_once('/').is_some_and(|(kind, subtype)| {
        !kind.is_empty()
            && !subtype.is_empty()
            && kind.len() <= 127
            && subtype.len() <= 127
            && kind.bytes().all(valid_mime_byte)
            && subtype.bytes().all(valid_mime_byte)
    })
}

fn valid_mime_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(
            byte,
            b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
        )
}

pub fn hello_extension_id(params: &Value) -> Result<&str, RpcValidationError> {
    required_bounded_string(required_object_value(params, "extension")?, "id", 32)
}

pub(crate) fn hello_install_instance_id(params: &Value) -> Result<&str, RpcValidationError> {
    let value = required_bounded_string(
        required_object_value(params, "extension")?,
        "install_instance_id",
        256,
    )?;
    valid_opaque_id(value)
        .then_some(value)
        .ok_or(RpcValidationError::ParamsInvalid)
}

pub fn request_timeout_ms(request: &RpcRequest) -> Result<u64, RpcValidationError> {
    if request.method == "bridge.hello" {
        return Ok(HELLO_TIMEOUT_MS);
    }
    let timeout = request
        .params
        .as_object()
        .and_then(|fields| fields.get("timeout_ms"))
        .and_then(Value::as_u64)
        .unwrap_or(MAX_REQUEST_TIMEOUT_MS);
    if timeout == 0 || timeout > MAX_REQUEST_TIMEOUT_MS {
        return Err(RpcValidationError::DeadlineInvalid);
    }
    Ok(timeout)
}

pub fn extension_id_from_origin(origin: &str) -> Option<&str> {
    let value = origin
        .strip_prefix("chrome-extension://")?
        .strip_suffix('/')?;
    is_extension_id(value).then_some(value)
}

pub(crate) fn is_extension_id(value: &str) -> bool {
    value.len() == 32 && value.bytes().all(|byte| matches!(byte, b'a'..=b'p'))
}

fn require_contract(fields: &BTreeMap<String, Value>) -> Result<(), RpcValidationError> {
    if required_u64(fields, "contract_version")? != CONTRACT_VERSION {
        return Err(RpcValidationError::VersionMismatch);
    }
    Ok(())
}

fn required_object<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a BTreeMap<String, Value>, RpcValidationError> {
    fields
        .get(key)
        .and_then(Value::as_object)
        .ok_or(RpcValidationError::ParamsInvalid)
}

fn required_object_value<'a>(
    value: &'a Value,
    key: &str,
) -> Result<&'a BTreeMap<String, Value>, RpcValidationError> {
    value
        .as_object()
        .and_then(|fields| fields.get(key))
        .and_then(Value::as_object)
        .ok_or(RpcValidationError::ParamsInvalid)
}

fn required_array<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a [Value], RpcValidationError> {
    fields
        .get(key)
        .and_then(Value::as_array)
        .ok_or(RpcValidationError::ParamsInvalid)
}

fn required_string<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    max_bytes: usize,
) -> Result<&'a str, RpcValidationError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| value.len() <= max_bytes)
        .ok_or(RpcValidationError::ParamsInvalid)
}

fn required_bounded_string<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    max_bytes: usize,
) -> Result<&'a str, RpcValidationError> {
    let value = required_string(fields, key, max_bytes)?;
    if bounded_identifier(value, max_bytes) {
        Ok(value)
    } else {
        Err(RpcValidationError::ParamsInvalid)
    }
}

fn required_u64(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, RpcValidationError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(RpcValidationError::ParamsInvalid)
}

fn required_opaque_id<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a str, RpcValidationError> {
    let value = required_bounded_string(fields, key, 256)?;
    valid_opaque_id(value)
        .then_some(value)
        .ok_or(RpcValidationError::ParamsInvalid)
}

fn require_exact_string(
    fields: &BTreeMap<String, Value>,
    key: &str,
    expected: &str,
) -> Result<(), RpcValidationError> {
    if fields.get(key).and_then(Value::as_str) != Some(expected) {
        return Err(RpcValidationError::EnvelopeInvalid);
    }
    Ok(())
}

fn validate_string_array(
    fields: &BTreeMap<String, Value>,
    key: &str,
    max_items: usize,
    max_item_bytes: usize,
    allowlist: Option<&[&str]>,
) -> Result<(), RpcValidationError> {
    let values = required_array(fields, key)?;
    if values.len() > max_items
        || values.iter().any(|value| {
            !value.as_str().is_some_and(|value| {
                bounded_identifier(value, max_item_bytes)
                    && allowlist.is_none_or(|allowlist| allowlist.contains(&value))
            })
        })
    {
        return Err(RpcValidationError::ParamsInvalid);
    }
    Ok(())
}

fn bounded_identifier(value: &str, max_bytes: usize) -> bool {
    !value.is_empty()
        && value.len() <= max_bytes
        && !value.chars().any(|character| character.is_control())
}

fn bounded_rpc_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && value.len() <= 256
        && bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

pub(crate) fn valid_opaque_id(value: &str) -> bool {
    bounded_rpc_id(value)
}

pub(crate) fn valid_pairing_code(value: &str) -> bool {
    let groups = value.split('-').collect::<Vec<_>>();
    if groups.len() != 3 || groups[0] != "A0B1" {
        return false;
    }
    groups[1].len() == 8
        && groups[1]
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'A'..=b'F'))
        && groups[2].len() == 32
        && groups[2].bytes().all(|byte| {
            matches!(
                byte,
                b'0'..=b'9'
                    | b'A'..=b'H'
                    | b'J'
                    | b'K'
                    | b'M'
                    | b'N'
                    | b'P'..=b'T'
                    | b'V'..=b'Z'
            )
        })
}

pub(crate) fn valid_http_origin(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 512
        || value.bytes().any(|byte| {
            byte.is_ascii_control() || matches!(byte, b' ' | b'\\' | b'%' | b'?' | b'#' | b'@')
        })
    {
        return false;
    }

    let (scheme, authority, default_port) = if let Some(authority) = value.strip_prefix("https://")
    {
        ("https", authority, 443)
    } else if let Some(authority) = value.strip_prefix("http://") {
        ("http", authority, 80)
    } else {
        return false;
    };
    if authority.is_empty()
        || authority.contains('/')
        || authority.contains('@')
        || authority.bytes().any(|byte| {
            byte.is_ascii_control() || matches!(byte, b' ' | b'\\' | b'%' | b'?' | b'#')
        })
    {
        return false;
    }
    let Some((host, port)) = split_host_port(authority) else {
        return false;
    };
    if port == Some(0) || port == Some(default_port) || !valid_host(host) {
        return false;
    }

    let canonical_host = match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(address)) => address.to_string(),
        Ok(IpAddr::V6(address)) => format!("[{}]", address),
        Err(_) if host == host.to_ascii_lowercase() => host.to_owned(),
        Err(_) => return false,
    };
    let canonical_authority = port.map_or_else(
        || canonical_host.clone(),
        |port| format!("{canonical_host}:{port}"),
    );
    value == format!("{scheme}://{canonical_authority}")
}

fn valid_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

pub(crate) fn valid_server_base_origin(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 2_048
        || value.bytes().any(|byte| {
            byte.is_ascii_control() || matches!(byte, b' ' | b'\\' | b'%' | b'?' | b'#')
        })
    {
        return false;
    }

    let (scheme, remainder) = if let Some(remainder) = value.strip_prefix("https://") {
        ("https", remainder)
    } else if let Some(remainder) = value.strip_prefix("http://") {
        ("http", remainder)
    } else {
        return false;
    };
    let (authority, path) = remainder
        .split_once('/')
        .map_or((remainder, ""), |(authority, path)| (authority, path));
    if authority.is_empty() || authority.contains('@') {
        return false;
    }
    let Some((host, port)) = split_host_port(authority) else {
        return false;
    };
    if port == Some(0) || !valid_host(host) || !valid_base_path(path) {
        return false;
    }
    scheme == "https" || is_loopback_host(host)
}

#[cfg(not(feature = "local-development"))]
fn valid_pairing_server_base_origin(value: &str) -> bool {
    valid_server_base_origin(value)
}

#[cfg(feature = "local-development")]
fn valid_pairing_server_base_origin(value: &str) -> bool {
    valid_development_server_base_origin(value)
}

#[cfg(feature = "local-development")]
pub(crate) fn valid_development_server_base_origin(value: &str) -> bool {
    let Some(authority) = value.strip_prefix("http://") else {
        return false;
    };
    if authority.contains('/') || !valid_server_base_origin(value) {
        return false;
    }
    let Some((host, Some(port))) = split_host_port(authority) else {
        return false;
    };
    if port == 80 || !is_loopback_host(host) {
        return false;
    }
    let canonical_host = match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(address)) => address.to_string(),
        Ok(IpAddr::V6(address)) => format!("[{address}]"),
        Err(_) if host == host.to_ascii_lowercase() => host.to_owned(),
        Err(_) => return false,
    };
    value == format!("http://{canonical_host}:{port}")
}

fn split_host_port(authority: &str) -> Option<(&str, Option<u16>)> {
    if let Some(bracketed) = authority.strip_prefix('[') {
        let close = bracketed.find(']')?;
        let host = bracketed.get(..close)?;
        if !matches!(host.parse::<IpAddr>().ok()?, IpAddr::V6(_)) {
            return None;
        }
        let suffix = bracketed.get(close + 1..)?;
        let port = if suffix.is_empty() {
            None
        } else {
            Some(suffix.strip_prefix(':')?.parse::<u16>().ok()?)
        };
        return Some((host, port));
    }
    if authority.matches(':').count() > 1 {
        return None;
    }
    match authority.rsplit_once(':') {
        Some((host, raw_port)) if !host.is_empty() && !raw_port.is_empty() => {
            Some((host, Some(raw_port.parse::<u16>().ok()?)))
        }
        Some(_) => None,
        None => Some((authority, None)),
    }
}

fn valid_host(host: &str) -> bool {
    if host.is_empty() || host.len() > 253 || host.ends_with('.') {
        return false;
    }
    if host.parse::<IpAddr>().is_ok() {
        return true;
    }
    if host
        .bytes()
        .all(|byte| byte.is_ascii_digit() || byte == b'.')
    {
        return false;
    }
    host.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && label
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            && label
                .as_bytes()
                .first()
                .is_some_and(|byte| byte.is_ascii_alphanumeric())
            && label
                .as_bytes()
                .last()
                .is_some_and(|byte| byte.is_ascii_alphanumeric())
    })
}

fn is_loopback_host(host: &str) -> bool {
    let normalized = host.to_ascii_lowercase();
    normalized == "localhost"
        || normalized.ends_with(".localhost")
        || normalized
            .parse::<IpAddr>()
            .is_ok_and(|address| address.is_loopback())
}

fn valid_base_path(path: &str) -> bool {
    path.is_empty()
        || (!path.starts_with('/')
            && !path.ends_with('/')
            && !path.contains("//")
            && path.split('/').all(|segment| {
                !matches!(segment, "" | "." | "..")
                    && segment.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'~' | b'-')
                    })
            }))
}

fn valid_sha256_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    })
}

fn lower_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn validate_bounded_tree(value: &Value, depth: usize) -> Result<(), RpcValidationError> {
    if depth > json::MAX_JSON_DEPTH {
        return Err(RpcValidationError::ParamsInvalid);
    }
    match value {
        Value::String(value) if value.len() > json::MAX_STRING_BYTES => {
            Err(RpcValidationError::ParamsInvalid)
        }
        Value::Array(values) => {
            if values.len() > json::MAX_CONTAINER_ITEMS {
                return Err(RpcValidationError::ParamsInvalid);
            }
            for value in values {
                validate_bounded_tree(value, depth + 1)?;
            }
            Ok(())
        }
        Value::Object(fields) => {
            if fields.len() > json::MAX_CONTAINER_ITEMS {
                return Err(RpcValidationError::ParamsInvalid);
            }
            for (key, value) in fields {
                if !bounded_identifier(key, 256) {
                    return Err(RpcValidationError::ParamsInvalid);
                }
                validate_bounded_tree(value, depth + 1)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const EXTENSION_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn hello(id: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{id}\",\"method\":\"bridge.hello\",\"params\":{{",
                "\"protocol\":\"a0.browser-bridge\",\"contract\":{{\"min\":1,\"max\":1}},",
                "\"extension\":{{\"id\":\"{extension}\",\"version\":\"0.1.0\",\"manifest_version\":3,",
                "\"install_instance_id\":\"install-1\",\"load_generation_id\":\"generation-1\"}},",
                "\"browser\":{{\"family\":\"chrome\",\"version\":\"146.0.0.0\"}},",
                "\"capabilities\":{{\"actions\":[\"open\"],\"features\":[\"tab_leases_v1\"],\"cdp_domains\":[]}},",
                "\"resume\":{{\"event_cursors\":[],\"inflight_op_ids\":[],\"lease_digest\":\"sha256:0000000000000000000000000000000000000000000000000000000000000000\"}}",
                "}}}}"
            ),
            id = id,
            extension = EXTENSION_ID,
        )
        .into_bytes()
    }

    fn resolve_challenge(decision: &str, grant: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"control-request-1\",",
                "\"method\":\"browser.resolve_challenge\",\"params\":{{",
                "\"contract_version\":1,\"control_id\":\"control-1\",",
                "\"challenge_id\":\"challenge-1\",\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":\"op-1\",\"action_id\":\"action-1\",\"tab_handle\":\"tab-1\",",
                "\"document_id\":null,\"document_epoch\":0,",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"origin\":\"https://example.test\",",
                "\"action_class\":\"navigate\",\"decision\":\"{decision}\",\"grant\":{grant}",
                "}}}}"
            ),
            DIGEST = DIGEST,
            decision = decision,
            grant = grant,
        )
        .into_bytes()
    }

    fn resolve_action_challenge(decision: &str, grant: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"control-request-2\",",
                "\"method\":\"browser.resolve_challenge\",\"params\":{{",
                "\"contract_version\":1,\"control_id\":\"control-2\",",
                "\"challenge_id\":\"challenge-2\",\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":\"op-2\",\"action_id\":\"action-2\",\"tab_handle\":\"tab-1\",",
                "\"document_id\":\"document-1\",\"document_epoch\":1,",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"origin\":\"https://example.test\",",
                "\"action_class\":\"unknown\",\"data_classification\":\"none\",",
                "\"decision\":\"{decision}\",\"grant\":{grant}",
                "}}}}"
            ),
            DIGEST = DIGEST,
            decision = decision,
            grant = grant,
        )
        .into_bytes()
    }

    fn resolve_sensitive_action_challenge(decision: &str, grant: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"control-request-3\",",
                "\"method\":\"browser.resolve_challenge\",\"params\":{{",
                "\"contract_version\":1,\"control_id\":\"control-3\",",
                "\"challenge_id\":\"challenge-3\",\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":\"op-3\",\"action_id\":\"action-3\",\"tab_handle\":\"tab-1\",",
                "\"document_id\":\"document-1\",\"document_epoch\":1,",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",\"origin\":\"https://example.test\",",
                "\"action_class\":\"sensitive_input\",\"data_classification\":{{",
                "\"kind\":\"text\",\"sensitivity\":\"sensitive\",\"text_sha256\":\"{DIGEST}\"}},",
                "\"decision\":\"{decision}\",\"grant\":{grant}",
                "}}}}"
            ),
            DIGEST = DIGEST,
            decision = decision,
            grant = grant,
        )
        .into_bytes()
    }

    fn type_perform(text: &str, digest: Option<&str>) -> Vec<u8> {
        let Value::Object(mut envelope) = json::parse(include_bytes!(
            "../tests/fixtures/native-rpc-v1/browser-perform.valid.json"
        ))
        .unwrap() else {
            panic!()
        };
        let params = envelope
            .get_mut("params")
            .and_then(|value| match value {
                Value::Object(fields) => Some(fields),
                _ => None,
            })
            .unwrap();
        params.insert("action".into(), Value::String("type".into()));
        let text_digest = digest
            .map(str::to_owned)
            .unwrap_or_else(|| lower_hex(&Sha256::digest(text.as_bytes())));
        params.insert(
            "args".into(),
            Value::Object(BTreeMap::from([
                ("ref".into(), Value::String("frame0:node24".into())),
                ("text".into(), Value::String(text.into())),
                ("text_sha256".into(), Value::String(text_digest)),
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
                Value::String("semantic_refs_v1".into()),
                Value::String("trusted_input_v1".into()),
            ]),
        );
        Value::Object(envelope).encode().into_bytes()
    }

    fn artifact_chunk(data: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"artifact-rpc-1\",",
                "\"method\":\"artifact.chunk\",\"params\":{{\"contract_version\":1,",
                "\"context_id\":\"context-1\",\"browser_session_id\":\"session-1\",",
                "\"turn_id\":\"turn-1\",\"action_id\":\"action-1\",\"op_id\":\"op-1\",",
                "\"artifact_id\":\"artifact-1\",\"direction\":\"output\",",
                "\"purpose\":\"screenshot\",\"chunk_index\":0,\"data\":\"{data}\"}}}}"
            ),
            data = data,
        )
        .into_bytes()
    }

    #[test]
    fn strict_hello_parses_and_exposes_declared_extension() {
        let fixture = include_bytes!("../tests/fixtures/native-rpc-v1/hello.valid.json");
        let RpcMessage::Request(fixture_request) = parse_message(fixture, Peer::Extension).unwrap()
        else {
            panic!("expected request")
        };
        assert_eq!(
            hello_extension_id(&fixture_request.params),
            Ok(EXTENSION_ID)
        );

        let RpcMessage::Request(request) =
            parse_message(&hello("hello-1"), Peer::Extension).unwrap()
        else {
            panic!("expected request")
        };
        assert_eq!(request.id.as_deref(), Some("hello-1"));
        assert_eq!(hello_extension_id(&request.params), Ok(EXTENSION_ID));
        assert_eq!(hello_install_instance_id(&request.params), Ok("install-1"));

        let invalid_profile = String::from_utf8(hello("hello-2"))
            .unwrap()
            .replace("install-1", "profile with spaces");
        assert_eq!(
            parse_message(invalid_profile.as_bytes(), Peer::Extension),
            Err(RpcValidationError::ParamsInvalid)
        );
    }

    #[test]
    fn checked_in_server_fixture_obeys_the_directional_allowlist() {
        let fixture = include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json");
        assert!(matches!(
            parse_message(fixture, Peer::Server),
            Ok(RpcMessage::Request(_))
        ));
        assert_eq!(
            parse_message(fixture, Peer::Extension),
            Err(RpcValidationError::MethodNotAllowed)
        );
    }

    #[test]
    fn pairing_exchange_requires_the_canonical_code_shape() {
        let valid = include_bytes!("../tests/fixtures/native-rpc-v1/pairing-exchange.valid.json");
        assert!(parse_message(valid, Peer::Extension).is_ok());
        let invalid = br#"{"jsonrpc":"2.0","id":"1","method":"pairing.exchange","params":{"contract_version":1,"pairing_code":"A0B1-AB12-ABCDEFGH","server_base_origin":"https://agent.example.test"}}"#;
        assert_eq!(
            parse_message(invalid, Peer::Extension),
            Err(RpcValidationError::ParamsInvalid)
        );
    }

    #[test]
    fn pairing_exchange_requires_exact_keys_and_a_safe_server_origin() {
        let extra =
            include_bytes!("../tests/fixtures/native-rpc-v1/pairing-exchange-extra.invalid.json");
        assert_eq!(
            parse_message(extra, Peer::Extension),
            Err(RpcValidationError::ParamsInvalid)
        );

        for origin in [
            "https://agent.example.test",
            "https://agent.example.test:8443/a0_v1/~bridge",
            "https://192.0.2.4/a0",
            "http://localhost:50080",
            "http://dev.localhost/a0",
            "http://127.9.8.7:50080/a0",
            "http://[::1]:50080/a0",
        ] {
            assert!(valid_server_base_origin(origin), "expected valid: {origin}");
        }
        for origin in [
            "http://agent.example.test",
            "http://localhost.example.test",
            "http://127.0.0.1.example.test",
            "http://128.0.0.1",
            "http://[2001:db8::1]",
            "https://user:password@agent.example.test",
            "https://agent.example.test?mode=unsafe",
            "https://agent.example.test#fragment",
            "https://agent.example.test/a%2fb",
            "https://agent.example.test//a0",
            "https://agent.example.test/a0//bridge",
            "https://agent.example.test/./a0",
            "https://agent.example.test/a0/../admin",
            "https://agent.example.test/a0!",
            "https://agent.example.test/a0/",
            "https://agent.example.test:0",
            "https://127.0.0.999",
            "https://*.example.test",
            "https://example..test",
            "ftp://agent.example.test",
        ] {
            assert!(
                !valid_server_base_origin(origin),
                "expected invalid: {origin}"
            );
        }
    }

    #[cfg(feature = "local-development")]
    #[test]
    fn development_pairing_requires_exact_canonical_explicit_port_loopback_http() {
        for origin in [
            "http://localhost:50080",
            "http://dev.localhost:50080",
            "http://127.9.8.7:50080",
            "http://[::1]:50080",
        ] {
            assert!(
                valid_development_server_base_origin(origin),
                "expected valid: {origin}"
            );
        }
        for origin in [
            "https://localhost:50080",
            "http://localhost",
            "http://localhost:80",
            "http://localhost:50080/",
            "http://LOCALHOST:50080",
            "http://127.1:50080",
            "http://192.0.2.1:50080",
        ] {
            assert!(
                !valid_development_server_base_origin(origin),
                "expected invalid: {origin}"
            );
        }
    }

    #[test]
    fn challenge_resolution_requires_exact_navigation_and_typed_grant_binding() {
        let allow_once = resolve_challenge(
            "allow_once",
            r#"{"origin_grant_id":"grant-1","scope":"operation","origin":"https://example.test","expires_at_ms":2000}"#,
        );
        assert!(parse_message(&allow_once, Peer::Server).is_ok());
        assert!(parse_message(&resolve_challenge("deny", "null"), Peer::Server).is_ok());

        for invalid in [
            resolve_challenge("allow_once", "null"),
            resolve_challenge(
                "allow_turn",
                r#"{"origin_grant_id":"grant-1","scope":"operation","origin":"https://example.test","expires_at_ms":2000}"#,
            ),
            resolve_challenge(
                "deny",
                r#"{"origin_grant_id":"grant-1","scope":"operation","origin":"https://example.test","expires_at_ms":2000}"#,
            ),
        ] {
            assert_eq!(
                parse_message(&invalid, Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }

        let extra = String::from_utf8(allow_once.clone()).unwrap().replace(
            "\"document_epoch\":0",
            "\"document_epoch\":0,\"summary\":\"not allowed\"",
        );
        assert_eq!(
            parse_message(extra.as_bytes(), Peer::Server),
            Err(RpcValidationError::ParamsInvalid)
        );
        let mismatched_origin = String::from_utf8(allow_once).unwrap().replace(
            "\"origin\":\"https://example.test\",\"expires_at_ms\"",
            "\"origin\":\"https://other.test\",\"expires_at_ms\"",
        );
        assert_eq!(
            parse_message(mismatched_origin.as_bytes(), Peer::Server),
            Err(RpcValidationError::ParamsInvalid)
        );

        let uppercase_hash = String::from_utf8(resolve_challenge("deny", "null"))
            .unwrap()
            .replacen(DIGEST, &DIGEST.to_uppercase(), 1);
        assert_eq!(
            parse_message(uppercase_hash.as_bytes(), Peer::Server),
            Err(RpcValidationError::ParamsInvalid)
        );

        let extra_grant = resolve_challenge(
            "allow_once",
            r#"{"origin_grant_id":"grant-1","scope":"operation","origin":"https://example.test","expires_at_ms":2000,"source":"model"}"#,
        );
        assert_eq!(
            parse_message(&extra_grant, Peer::Server),
            Err(RpcValidationError::ParamsInvalid)
        );
    }

    #[test]
    fn action_challenge_resolution_requires_exact_once_grant_binding() {
        let grant = format!(
            concat!(
                "{{\"action_grant_id\":\"grant-2\",\"scope\":\"operation\",",
                "\"origin\":\"https://example.test\",\"action_class\":\"unknown\",",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",",
                "\"data_classification\":\"none\",\"expires_at_ms\":2000}}"
            ),
            DIGEST = DIGEST,
        );
        assert!(parse_message(
            &resolve_action_challenge("approve_once", &grant),
            Peer::Server
        )
        .is_ok());
        assert!(parse_message(&resolve_action_challenge("decline", "null"), Peer::Server).is_ok());

        for invalid in [
            resolve_action_challenge("approve_once", "null"),
            resolve_action_challenge("decline", &grant),
            resolve_action_challenge("allow_once", &grant),
            resolve_action_challenge(
                "approve_once",
                &grant.replace("https://example.test", "https://other.test"),
            ),
            resolve_action_challenge(
                "approve_once",
                &grant.replace("\"unknown\"", "\"sensitive_input\""),
            ),
            resolve_action_challenge("approve_once", &grant.replace("\"none\"", "\"text\"")),
        ] {
            assert_eq!(
                parse_message(&invalid, Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }

        for (before, after) in [
            ("\"document_id\":\"document-1\"", "\"document_id\":null"),
            (
                "\"data_classification\":\"none\"",
                "\"data_classification\":\"none\",\"summary\":\"not allowed\"",
            ),
        ] {
            let invalid = String::from_utf8(resolve_action_challenge("decline", "null"))
                .unwrap()
                .replace(before, after);
            assert_eq!(
                parse_message(invalid.as_bytes(), Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }
    }

    #[test]
    fn type_sensitive_text_resolution_requires_exact_tagged_classification_binding() {
        let classification = format!(
            "{{\"kind\":\"text\",\"sensitivity\":\"sensitive\",\"text_sha256\":\"{DIGEST}\"}}"
        );
        let grant = format!(
            concat!(
                "{{\"action_grant_id\":\"grant-3\",\"scope\":\"operation\",",
                "\"origin\":\"https://example.test\",\"action_class\":\"sensitive_input\",",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",",
                "\"data_classification\":{classification},\"expires_at_ms\":2000}}"
            ),
            DIGEST = DIGEST,
            classification = classification,
        );
        assert!(parse_message(
            &resolve_sensitive_action_challenge("approve_once", &grant),
            Peer::Server,
        )
        .is_ok());
        assert!(parse_message(
            &resolve_sensitive_action_challenge("decline", "null"),
            Peer::Server,
        )
        .is_ok());

        let other_digest = "f".repeat(64);
        for invalid in [
            resolve_sensitive_action_challenge(
                "approve_once",
                &grant.replace(
                    &format!("\"text_sha256\":\"{DIGEST}\""),
                    &format!("\"text_sha256\":\"{other_digest}\""),
                ),
            ),
            resolve_sensitive_action_challenge(
                "approve_once",
                &grant.replace(
                    "\"sensitivity\":\"sensitive\"",
                    "\"sensitivity\":\"public\"",
                ),
            ),
            resolve_sensitive_action_challenge("decline", &grant),
        ] {
            assert_eq!(
                parse_message(&invalid, Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }

        let uppercase_digest = DIGEST.to_uppercase();
        let uppercase = String::from_utf8(resolve_sensitive_action_challenge("decline", "null"))
            .unwrap()
            .replace(
                &format!("\"text_sha256\":\"{DIGEST}\""),
                &format!("\"text_sha256\":\"{uppercase_digest}\""),
            );
        assert_eq!(
            parse_message(uppercase.as_bytes(), Peer::Server),
            Err(RpcValidationError::ParamsInvalid)
        );
        let extra = String::from_utf8(resolve_sensitive_action_challenge("decline", "null"))
            .unwrap()
            .replace("\"text_sha256\":", "\"source\":\"model\",\"text_sha256\":");
        assert_eq!(
            parse_message(extra.as_bytes(), Peer::Server),
            Err(RpcValidationError::ParamsInvalid)
        );
    }

    #[test]
    fn click_perform_requires_exact_semantic_args_target_and_no_preauthorization() {
        let valid = include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json");
        assert!(parse_message(valid, Peer::Server).is_ok());
        let valid = String::from_utf8(valid.to_vec()).unwrap();
        for invalid in [
            valid.replace(
                "\"expected_action_class\": \"unknown\"",
                "\"expected_action_class\": \"unknown\", \"selector\": \"button\"",
            ),
            valid.replace(
                "\"expected_action_class\": \"unknown\"",
                "\"expected_action_class\": \"safe\"",
            ),
            valid.replace(
                "\"target\": { \"tab_handle\"",
                "\"target\": { \"extra\": true, \"tab_handle\"",
            ),
            valid.replace(
                "\"action_grant_id\": null",
                "\"action_grant_id\": \"forged\"",
            ),
            valid.replace(
                "\"origin_grant_id\": \"origin-grant-1\"",
                "\"origin_grant_id\": null",
            ),
            valid.replace(
                "\"required_capabilities\": [\"click\",",
                "\"required_capabilities\": [",
            ),
        ] {
            assert_eq!(
                parse_message(invalid.as_bytes(), Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }
    }

    #[test]
    fn type_perform_recomputes_exact_bounded_utf8_digest_without_normalization() {
        let valid = type_perform("secret 🚀\nline", None);
        assert!(parse_message(&valid, Peer::Server).is_ok());
        assert!(parse_message(&type_perform(&"é".repeat(16_384), None), Peer::Server).is_ok());

        for invalid in [
            type_perform("", None),
            type_perform("secret", Some(DIGEST)),
            type_perform("secret", Some(&DIGEST.to_uppercase())),
            type_perform("bad\0text", None),
            type_perform("bad\rtext", None),
            type_perform(&("é".repeat(16_384) + "a"), None),
        ] {
            assert_eq!(
                parse_message(&invalid, Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }

        let valid = String::from_utf8(type_perform("secret", None)).unwrap();
        for invalid in [
            valid.replace(
                "\"expected_action_class\":\"sensitive_input\"",
                "\"expected_action_class\":\"unknown\"",
            ),
            valid.replace(
                "\"expected_action_class\":\"sensitive_input\"",
                "\"expected_action_class\":\"sensitive_input\",\"selector\":\"input\"",
            ),
            valid.replace("\"action_grant_id\":null", "\"action_grant_id\":\"forged\""),
            valid.replace("\"type\",\"semantic_refs_v1\"", "\"semantic_refs_v1\""),
        ] {
            assert_eq!(
                parse_message(invalid.as_bytes(), Peer::Server),
                Err(RpcValidationError::ParamsInvalid)
            );
        }
    }

    #[test]
    fn browser_origins_are_exact_canonical_url_origins() {
        for origin in [
            "https://example.test",
            "https://example.test:8443",
            "http://127.0.0.1:50080",
            "http://[::1]:50080",
        ] {
            assert!(valid_http_origin(origin), "expected valid: {origin}");
        }
        for origin in [
            "HTTPS://example.test",
            "https://EXAMPLE.test",
            "https://example.test/",
            "https://example.test:443",
            "https://example.test:0443",
            "https://user@example.test",
            "https://example.test?query",
            "https://127.000.000.001",
            "https://[0:0:0:0:0:0:0:1]",
        ] {
            assert!(!valid_http_origin(origin), "expected invalid: {origin}");
        }
    }

    #[test]
    fn correlation_ids_use_the_extension_symbolic_form() {
        for id in ["a", "1", "rpc:request-1", "a.b_c-d:e"] {
            let message = format!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{id}\",\"method\":\"bridge.ping\",\"params\":{{}}}}"
            );
            assert!(parse_message(message.as_bytes(), Peer::Extension).is_ok());
        }
        for id in ["", "_leading", "-leading", "has/slash", "has space", "é"] {
            let message = format!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"{id}\",\"method\":\"bridge.ping\",\"params\":{{}}}}"
            );
            assert_eq!(
                parse_message(message.as_bytes(), Peer::Extension),
                Err(RpcValidationError::IdentifierInvalid),
                "expected invalid: {id}"
            );
        }
        let oversized = format!("a{}", "b".repeat(256));
        let message = format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":\"{oversized}\",\"method\":\"bridge.ping\",\"params\":{{}}}}"
        );
        assert_eq!(
            parse_message(message.as_bytes(), Peer::Extension),
            Err(RpcValidationError::IdentifierInvalid)
        );
        assert_eq!(
            parse_message(
                br#"{"jsonrpc":"2.0","id":"_response","result":{}}"#,
                Peer::Server,
            ),
            Err(RpcValidationError::IdentifierInvalid)
        );
    }

    #[test]
    fn batch_unknown_method_and_wrong_request_kind_fail_closed() {
        assert_eq!(
            parse_message(br#"[]"#, Peer::Extension),
            Err(RpcValidationError::BatchForbidden)
        );
        assert_eq!(
            parse_message(
                br#"{"jsonrpc":"2.0","id":"1","method":"shell.run","params":{}}"#,
                Peer::Extension,
            ),
            Err(RpcValidationError::MethodNotAllowed)
        );
        assert_eq!(
            parse_message(
                br#"{"jsonrpc":"2.0","method":"browser.perform","params":{}}"#,
                Peer::Server,
            ),
            Err(RpcValidationError::RequestKindInvalid)
        );
    }

    #[test]
    fn duplicate_envelope_fields_and_oversized_non_artifact_payload_fail() {
        assert!(matches!(
            parse_message(
                br#"{"jsonrpc":"2.0","jsonrpc":"2.0","id":"1","method":"bridge.ping","params":{}}"#,
                Peer::Extension,
            ),
            Err(RpcValidationError::JsonInvalid)
        ));
        let padding = "x".repeat(MAX_NON_ARTIFACT_BYTES);
        let message = format!(
            "{{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"bridge.ping\",\"params\":{{\"nonce\":\"{padding}\"}}}}"
        );
        assert_eq!(
            parse_message(message.as_bytes(), Peer::Extension),
            Err(RpcValidationError::ParamsInvalid)
        );
    }

    #[test]
    fn artifact_chunk_is_bounded_below_native_frame_limit() {
        let data = "A".repeat(MAX_ARTIFACT_CHUNK_BASE64_BYTES + 1);
        let message = artifact_chunk(&data);
        assert_eq!(
            parse_message(&message, Peer::Extension),
            Err(RpcValidationError::ArtifactLimitInvalid)
        );
    }

    #[test]
    fn artifact_chunk_rejects_invalid_base64_and_accepts_the_raw_limit() {
        assert_eq!(
            parse_message(&artifact_chunk("not_base64"), Peer::Extension),
            Err(RpcValidationError::ParamsInvalid)
        );

        let data = "A".repeat(MAX_ARTIFACT_CHUNK_BASE64_BYTES);
        assert!(parse_message(&artifact_chunk(&data), Peer::Extension).is_ok());
    }
}
