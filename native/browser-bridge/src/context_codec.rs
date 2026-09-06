//! Bounded context transport. Core remains the authorization/projector owner;
//! this codec additionally confines context traffic to this connection's list.

use crate::connector_codec::{CodecError, HANDLER_ID};
use crate::json::{self, Value};
use crate::rpc::{self, Peer, RpcErrorObject, RpcMessage, RpcRequest, RpcResponse};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

const MAX_PENDING: usize = 64;
const MAX_CONTEXTS: usize = 64;
const MAX_COMPLETED: usize = 2048;

struct Pending {
    native_id: String,
    correlation: String,
    method: String,
    context_id: Option<String>,
    client_message_id: Option<String>,
    item_id: Option<String>,
    challenge_id: Option<String>,
    decision: Option<String>,
    rotation_id: Option<String>,
}

pub(crate) struct ContextCodec {
    next_ack: u32,
    pending: BTreeMap<u32, Pending>,
    completed: VecDeque<u32>,
    advertised: BTreeSet<String>,
    subscribed: BTreeSet<String>,
    subscription_requests: BTreeMap<String, u32>,
}

impl ContextCodec {
    pub(crate) fn new() -> Self {
        Self {
            next_ack: 2,
            pending: BTreeMap::new(),
            completed: VecDeque::new(),
            advertised: BTreeSet::new(),
            subscribed: BTreeSet::new(),
            subscription_requests: BTreeMap::new(),
        }
    }

    pub(crate) fn request(&mut self, payload: &[u8]) -> Result<String, CodecError> {
        let RpcMessage::Request(request) =
            rpc::parse_message(payload, Peer::Extension).map_err(|_| CodecError::InvalidPacket)?
        else {
            return Err(CodecError::InvalidPacket);
        };
        if request.method.starts_with("credential.") {
            return Err(CodecError::InvalidPacket);
        }
        self.encode_request(request)
    }

    pub(crate) fn credential_request(
        &mut self,
        payload: &[u8],
        document: Value,
    ) -> Result<String, CodecError> {
        let RpcMessage::Request(mut request) =
            rpc::parse_message(payload, Peer::Extension).map_err(|_| CodecError::InvalidPacket)?
        else {
            return Err(CodecError::InvalidPacket);
        };
        let fields = request
            .params
            .as_object()
            .ok_or(CodecError::InvalidPacket)?;
        if !request.method.starts_with("credential.")
            || fields.len() != 1
            || fields.get("contract_version").and_then(Value::as_u64) != Some(1)
        {
            return Err(CodecError::InvalidPacket);
        }
        request.params = document;
        self.encode_request(request)
    }

    fn encode_request(&mut self, request: RpcRequest) -> Result<String, CodecError> {
        let native_id = request.id.ok_or(CodecError::InvalidPacket)?;
        let fields = request
            .params
            .as_object()
            .ok_or(CodecError::InvalidPacket)?;
        if fields.get("contract_version").and_then(Value::as_u64) != Some(1) {
            return Err(CodecError::InvalidPacket);
        }
        let event = match request.method.as_str() {
            "context.list" => {
                exact_keys(fields, &["contract_version", "limit"])?;
                if fields
                    .get("limit")
                    .is_some_and(|v| v.as_u64().is_none_or(|v| !(1..=64).contains(&v)))
                {
                    return Err(CodecError::InvalidPacket);
                }
                "connector_context_list"
            }
            "context.subscribe" => {
                exact_keys(
                    fields,
                    &[
                        "contract_version",
                        "context_id",
                        "from",
                        "history",
                        "history_before",
                    ],
                )?;
                for key in ["from", "history_before"] {
                    if let Some(value) = fields.get(key) {
                        safe_integer(value)?;
                    }
                }
                if fields
                    .get("history")
                    .is_some_and(|v| !matches!(v.as_str(), Some("tail" | "all")))
                {
                    return Err(CodecError::InvalidPacket);
                }
                "connector_subscribe_context"
            }
            "context.unsubscribe" => {
                exact_keys(fields, &["contract_version", "context_id"])?;
                "connector_unsubscribe_context"
            }
            "context.send_message" | "context.queue_add" => {
                exact_keys(
                    fields,
                    &[
                        "contract_version",
                        "context_id",
                        "client_message_id",
                        "text",
                        "artifact_ids",
                        "tab_candidates",
                    ],
                )?;
                id(fields, "client_message_id")?;
                let text = fields
                    .get("text")
                    .and_then(Value::as_str)
                    .ok_or(CodecError::InvalidPacket)?;
                if text.trim().is_empty() || text.len() > 32768 {
                    return Err(CodecError::InvalidPacket);
                }
                for key in ["artifact_ids", "tab_candidates"] {
                    if request.method == "context.queue_add" && fields.contains_key(key) {
                        return Err(CodecError::InvalidPacket);
                    }
                    if fields
                        .get(key)
                        .is_some_and(|v| v.as_array().is_none_or(|v| !v.is_empty()))
                    {
                        return Err(CodecError::UnsupportedEvent);
                    }
                }
                if request.method == "context.queue_add" {
                    "connector_message_queue_add"
                } else {
                    "connector_send_message"
                }
            }
            "context.queue_remove" | "context.queue_send" => {
                exact_keys(fields, &["contract_version", "context_id", "item_id"])?;
                id(fields, "item_id")?;
                if request.method == "context.queue_remove" {
                    "connector_message_queue_remove"
                } else {
                    "connector_message_queue_send"
                }
            }
            "browser.approval_decision" => {
                exact_keys(
                    fields,
                    &["contract_version", "challenge_id", "kind", "decision"],
                )?;
                id(fields, "challenge_id")?;
                match (
                    fields.get("kind").and_then(Value::as_str),
                    fields.get("decision").and_then(Value::as_str),
                ) {
                    (Some("site"), Some("deny" | "allow_once" | "allow_turn"))
                    | (Some("action"), Some("decline" | "approve_once")) => {}
                    _ => return Err(CodecError::InvalidPacket),
                }
                "connector_browser_approval_decision"
            }
            "credential.rotate" | "credential.status" | "credential.revoke" => {
                let action = request
                    .method
                    .strip_prefix("credential.")
                    .ok_or(CodecError::InvalidPacket)?;
                if fields.get("action").and_then(Value::as_str) != Some(action) {
                    return Err(CodecError::InvalidPacket);
                }
                let expected = match action {
                    "rotate" => vec!["contract_version", "action", "rotation_id", "public_key"],
                    "status" => vec!["contract_version", "action", "rotation_id"],
                    _ => vec!["contract_version", "action"],
                };
                exact_keys(fields, &expected)?;
                if fields.len() != expected.len() {
                    return Err(CodecError::InvalidPacket);
                }
                if action != "revoke" {
                    id(fields, "rotation_id")?;
                }
                if action == "rotate" {
                    let key = fields
                        .get("public_key")
                        .and_then(Value::as_object)
                        .ok_or(CodecError::InvalidPacket)?;
                    exact_keys(key, &["algorithm", "encoding", "value"])?;
                    if key.len() != 3
                        || key.get("algorithm").and_then(Value::as_str) != Some("Ed25519")
                        || key.get("encoding").and_then(Value::as_str) != Some("raw-base64url")
                        || key.get("value").and_then(Value::as_str).is_none_or(|v| {
                            v.len() != 43
                                || !v
                                    .bytes()
                                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
                        })
                    {
                        return Err(CodecError::InvalidPacket);
                    }
                }
                "connector_bridge_credential_control"
            }
            _ => return Err(CodecError::UnsupportedEvent),
        };
        let context_id = if request.method.starts_with("credential.")
            || matches!(
                request.method.as_str(),
                "context.list" | "browser.approval_decision"
            ) {
            None
        } else {
            let context = id(fields, "context_id")?.to_owned();
            if !self.advertised.contains(&context) {
                return Err(CodecError::InvalidPacket);
            }
            Some(context)
        };
        if self.pending.len() >= MAX_PENDING {
            return Err(CodecError::Capacity);
        }
        if self
            .pending
            .values()
            .any(|item| item.native_id == native_id)
        {
            return Err(CodecError::InvalidPacket);
        }
        let ack = self.next_ack;
        self.next_ack = self.next_ack.checked_add(1).ok_or(CodecError::Capacity)?;
        let correlation = format!("native-context-{ack}");
        let packet = format!(
            "42/ws,{ack}{}",
            Value::Array(vec![
                Value::String(event.into()),
                Value::Object(BTreeMap::from([
                    ("correlationId".into(), Value::String(correlation.clone())),
                    ("data".into(), request.params.clone()),
                ]))
            ])
            .encode()
        );
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(CodecError::InvalidPacket);
        }
        if request.method == "context.unsubscribe" {
            if let Some(context) = &context_id {
                self.subscribed.remove(context);
                self.subscription_requests.remove(context);
            }
        }
        if request.method == "context.subscribe" {
            if let Some(context) = &context_id {
                self.subscription_requests.insert(context.clone(), ack);
            }
        }
        self.pending.insert(
            ack,
            Pending {
                native_id,
                correlation,
                method: request.method,
                context_id,
                client_message_id: fields
                    .get("client_message_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                item_id: fields
                    .get("item_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                challenge_id: fields
                    .get("challenge_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                decision: fields
                    .get("decision")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                rotation_id: fields
                    .get("rotation_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
            },
        );
        Ok(packet)
    }

    pub(crate) fn response(&mut self, packet: &str) -> Result<Option<Vec<u8>>, CodecError> {
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(CodecError::InvalidResponse);
        }
        let tail = packet
            .strip_prefix("43/ws,")
            .ok_or(CodecError::InvalidResponse)?;
        let split = tail.find('[').ok_or(CodecError::InvalidResponse)?;
        let number = &tail[..split];
        let ack: u32 = number.parse().map_err(|_| CodecError::InvalidResponse)?;
        if ack.to_string() != number {
            return Err(CodecError::InvalidResponse);
        }
        if self.completed.contains(&ack) {
            return Ok(None);
        }
        let pending = self.pending.get(&ack).ok_or(CodecError::InvalidResponse)?;
        let value =
            json::parse(tail[split..].as_bytes()).map_err(|_| CodecError::InvalidResponse)?;
        let args = value
            .as_array()
            .filter(|v| v.len() == 1)
            .ok_or(CodecError::InvalidResponse)?;
        let envelope = args[0].as_object().ok_or(CodecError::InvalidResponse)?;
        exact_keys(envelope, &["correlationId", "results"])?;
        if envelope.len() != 2 {
            return Err(CodecError::InvalidResponse);
        }
        if envelope.get("correlationId").and_then(Value::as_str) != Some(&pending.correlation) {
            return Err(CodecError::InvalidResponse);
        }
        let results = envelope
            .get("results")
            .and_then(Value::as_array)
            .filter(|v| v.len() == 1)
            .ok_or(CodecError::InvalidResponse)?;
        let result = results[0].as_object().ok_or(CodecError::InvalidResponse)?;
        exact_keys(
            result,
            &[
                "correlationId",
                "handlerId",
                "ok",
                "data",
                "error",
                "durationMs",
            ],
        )?;
        if result.get("correlationId").and_then(Value::as_str) != Some(&pending.correlation)
            || result.get("durationMs").is_some_and(|value| match value {
                Value::Number(number) => number.parse::<f64>().ok().is_none_or(|duration| {
                    !duration.is_finite() || !(0.0..=60000.0).contains(&duration)
                }),
                _ => true,
            })
        {
            return Err(CodecError::InvalidResponse);
        }
        if result.get("handlerId").and_then(Value::as_str) != Some(HANDLER_ID) {
            return Err(CodecError::InvalidResponse);
        }
        let result = match result.get("ok") {
            Some(Value::Bool(true)) => {
                let mut data = result
                    .get("data")
                    .and_then(Value::as_object)
                    .ok_or(CodecError::InvalidResponse)?
                    .clone();
                if pending.method.starts_with("credential.") {
                    validate_credential_status(&data)?;
                    if data.get("action").and_then(Value::as_str)
                        != pending.method.strip_prefix("credential.")
                        || data.get("rotation_id").and_then(Value::as_str)
                            != pending.rotation_id.as_deref()
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                } else if pending.method == "context.list" {
                    exact_keys(&data, &["contract_version", "contexts"])?;
                    let contexts = data
                        .get("contexts")
                        .and_then(Value::as_array)
                        .filter(|v| v.len() <= MAX_CONTEXTS)
                        .ok_or(CodecError::InvalidResponse)?;
                    let mut advertised = BTreeSet::new();
                    for context in contexts {
                        let context = context.as_object().ok_or(CodecError::InvalidResponse)?;
                        exact_keys(
                            context,
                            &[
                                "context_id",
                                "label",
                                "kind",
                                "status",
                                "created_at_ms",
                                "updated_at_ms",
                            ],
                        )?;
                        if context.len() != 6
                            || !advertised.insert(id(context, "context_id")?.to_owned())
                        {
                            return Err(CodecError::InvalidResponse);
                        }
                        bounded_text(context, "label", 256)?;
                        if !matches!(
                            context.get("kind").and_then(Value::as_str),
                            Some("chat" | "task")
                        ) || !matches!(
                            context.get("status").and_then(Value::as_str),
                            Some("idle" | "running" | "paused")
                        ) {
                            return Err(CodecError::InvalidResponse);
                        }
                        let created = safe_integer(
                            context
                                .get("created_at_ms")
                                .ok_or(CodecError::InvalidResponse)?,
                        )?;
                        let updated = safe_integer(
                            context
                                .get("updated_at_ms")
                                .ok_or(CodecError::InvalidResponse)?,
                        )?;
                        if updated < created {
                            return Err(CodecError::InvalidResponse);
                        }
                    }
                    self.advertised = advertised;
                    self.subscribed
                        .retain(|context| self.advertised.contains(context));
                    self.subscription_requests
                        .retain(|context, _| self.advertised.contains(context));
                } else if pending.method.starts_with("context.queue_") {
                    exact_keys(
                        &data,
                        &[
                            "contract_version",
                            "context_id",
                            "item_id",
                            "status",
                            "message_queue",
                        ],
                    )?;
                    if data.len() != 5
                        || data.get("context_id").and_then(Value::as_str)
                            != pending.context_id.as_deref()
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                    let item = id(&data, "item_id")?;
                    if pending
                        .item_id
                        .as_deref()
                        .is_some_and(|expected| expected != item)
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                    if !matches!(
                        data.get("status").and_then(Value::as_str),
                        Some("queued" | "removed" | "sent")
                    ) {
                        return Err(CodecError::InvalidResponse);
                    }
                    validate_queue(
                        data.get("message_queue")
                            .ok_or(CodecError::InvalidResponse)?,
                    )?;
                } else if pending.method == "browser.approval_decision" {
                    exact_keys(
                        &data,
                        &[
                            "contract_version",
                            "challenge_id",
                            "decision",
                            "control_id",
                            "status",
                            "expires_at_ms",
                        ],
                    )?;
                    let expected_decision = match pending.decision.as_deref() {
                        Some("approve_once") => "approved",
                        Some("decline") => "declined",
                        Some(value) => value,
                        None => return Err(CodecError::InvalidResponse),
                    };
                    let site = matches!(
                        pending.decision.as_deref(),
                        Some("deny" | "allow_once" | "allow_turn")
                    );
                    if data.len() != if site { 6 } else { 5 }
                        || data.get("challenge_id").and_then(Value::as_str)
                            != pending.challenge_id.as_deref()
                        || data.get("decision").and_then(Value::as_str) != Some(expected_decision)
                        || data.get("status").and_then(Value::as_str) != Some("accepted")
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                    id(&data, "control_id")?;
                    if site {
                        safe_integer(
                            data.get("expires_at_ms")
                                .ok_or(CodecError::InvalidResponse)?,
                        )?;
                    }
                } else {
                    exact_keys(
                        &data,
                        &[
                            "contract_version",
                            "context_id",
                            "subscribed",
                            "unsubscribed",
                            "last_sequence",
                            "history_before",
                            "has_more_history",
                            "complete",
                            "client_message_id",
                            "accepted",
                            "status",
                        ],
                    )?;
                    if data.get("context_id").and_then(Value::as_str)
                        != pending.context_id.as_deref()
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                    for key in ["last_sequence", "history_before"] {
                        if let Some(value) = data.get(key) {
                            safe_integer(value)?;
                        }
                    }
                    for key in [
                        "subscribed",
                        "unsubscribed",
                        "has_more_history",
                        "complete",
                        "accepted",
                    ] {
                        if data.get(key).is_some_and(|v| !matches!(v, Value::Bool(_))) {
                            return Err(CodecError::InvalidResponse);
                        }
                    }
                    if data.get("status").is_some_and(|v| {
                        !matches!(v.as_str(), Some("accepted" | "queued" | "running" | "idle"))
                    }) {
                        return Err(CodecError::InvalidResponse);
                    }
                    if pending.method == "context.subscribe" {
                        if data.get("subscribed") != Some(&Value::Bool(true)) {
                            return Err(CodecError::InvalidResponse);
                        }
                        if let Some(context) = &pending.context_id {
                            if self.subscription_requests.get(context) == Some(&ack) {
                                self.subscribed.insert(context.clone());
                            }
                        }
                    }
                    if pending.method == "context.send_message"
                        && data.get("client_message_id").and_then(Value::as_str)
                            != pending.client_message_id.as_deref()
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                    if pending.method == "context.unsubscribe"
                        && data.get("unsubscribed") != Some(&Value::Bool(true))
                    {
                        return Err(CodecError::InvalidResponse);
                    }
                }
                if data
                    .get("contract_version")
                    .is_some_and(|value| value.as_u64() != Some(1))
                {
                    return Err(CodecError::InvalidResponse);
                }
                data.insert("contract_version".into(), Value::Number("1".into()));
                Ok(Value::Object(data))
            }
            Some(Value::Bool(false)) => Err(RpcErrorObject {
                code: -32010,
                message: "The authorized context request was not completed.".into(),
                data: Some(Value::Object(BTreeMap::from([
                    ("a0_code".into(), Value::String("SCOPE_DENIED".into())),
                    (
                        "outcome".into(),
                        Value::String(
                            if pending.method == "context.send_message"
                                || pending.method.starts_with("context.queue_")
                                || pending.method == "browser.approval_decision"
                                || matches!(
                                    pending.method.as_str(),
                                    "credential.rotate" | "credential.revoke"
                                )
                            {
                                "unknown"
                            } else {
                                "not_applied"
                            }
                            .into(),
                        ),
                    ),
                    ("retryable".into(), Value::Bool(false)),
                    ("details".into(), Value::Object(BTreeMap::new())),
                ]))),
            }),
            _ => return Err(CodecError::InvalidResponse),
        };
        let bytes = RpcMessage::Response(RpcResponse {
            id: pending.native_id.clone(),
            result,
        })
        .encode();
        if bytes.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(CodecError::InvalidResponse);
        }
        self.pending.remove(&ack);
        self.remember(ack);
        Ok(Some(bytes))
    }

    pub(crate) fn notification(&mut self, packet: &str) -> Result<Option<Vec<u8>>, CodecError> {
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(CodecError::InvalidPacket);
        }
        let value = json::parse(
            packet
                .strip_prefix("42/ws,")
                .ok_or(CodecError::InvalidPacket)?
                .as_bytes(),
        )
        .map_err(|_| CodecError::InvalidPacket)?;
        let values = value
            .as_array()
            .filter(|v| v.len() == 2)
            .ok_or(CodecError::InvalidPacket)?;
        let method = match values[0].as_str() {
            Some("connector_context_snapshot") => "context.snapshot",
            Some("connector_context_event") => "context.event",
            Some("connector_message_queue_updated") => "context.queue_updated",
            Some("connector_bridge_credential_status") => "credential.changed",
            Some("connector_context_complete" | "connector_context_error") => "context.complete",
            _ => return Err(CodecError::UnsupportedEvent),
        };
        let envelope = values[1].as_object().ok_or(CodecError::InvalidPacket)?;
        if envelope.get("handlerId").and_then(Value::as_str) != Some(HANDLER_ID) {
            return Err(CodecError::InvalidPacket);
        }
        let mut data = envelope
            .get("data")
            .and_then(Value::as_object)
            .ok_or(CodecError::InvalidPacket)?
            .clone();
        if method == "credential.changed" {
            validate_credential_status(&data)?;
            if data.get("action").and_then(Value::as_str) != Some("revoke")
                || data.get("status").and_then(Value::as_str) != Some("revoked")
            {
                return Err(CodecError::InvalidPacket);
            }
            // Core emits its durable self-revocation receipt before disconnect.
            // Complete only this port's explicit pending revoke, if any, because
            // the later Socket.IO ACK may be overtaken by that disconnect.
            if let Some(ack) = self
                .pending
                .iter()
                .find_map(|(ack, item)| (item.method == "credential.revoke").then_some(*ack))
            {
                let pending = self.pending.remove(&ack).ok_or(CodecError::InvalidPacket)?;
                self.remember(ack);
                return Ok(Some(
                    RpcMessage::Response(RpcResponse {
                        id: pending.native_id,
                        result: Ok(Value::Object(data)),
                    })
                    .encode(),
                ));
            }
            return Ok(Some(
                RpcMessage::Request(RpcRequest {
                    id: None,
                    method: method.into(),
                    params: Value::Object(data),
                })
                .encode(),
            ));
        }
        let context = id(&data, "context_id")?;
        if !self.advertised.contains(context)
            || !self.subscription_requests.contains_key(context)
            || !(self.subscribed.contains(context)
                || self.pending.values().any(|p| {
                    p.method == "context.subscribe" && p.context_id.as_deref() == Some(context)
                }))
        {
            // In-flight frames can arrive after a panel unsubscribes or its
            // list changes. Drop presentation traffic without closing the
            // native port or interrupting unrelated browser work.
            return Ok(None);
        }
        match method {
            "context.queue_updated" => {
                exact_keys(&data, &["contract_version", "context_id", "message_queue"])?;
                if data.len() != 3 {
                    return Err(CodecError::InvalidPacket);
                }
                validate_queue(data.get("message_queue").ok_or(CodecError::InvalidPacket)?)?;
            }
            "context.snapshot" => {
                exact_keys(
                    &data,
                    &[
                        "contract_version",
                        "context_id",
                        "events",
                        "last_sequence",
                        "complete",
                        "history_before",
                        "has_more_history",
                    ],
                )?;
                safe_integer(data.get("last_sequence").ok_or(CodecError::InvalidPacket)?)?;
                if !matches!(data.get("complete"), Some(Value::Bool(_))) {
                    return Err(CodecError::InvalidPacket);
                }
                if data.contains_key("history_before") != data.contains_key("has_more_history") {
                    return Err(CodecError::InvalidPacket);
                }
                if let Some(cursor) = data.get("history_before") {
                    safe_integer(cursor)?;
                    if !matches!(data.get("has_more_history"), Some(Value::Bool(_))) {
                        return Err(CodecError::InvalidPacket);
                    }
                }
                for event in data
                    .get("events")
                    .and_then(Value::as_array)
                    .filter(|v| v.len() <= 50)
                    .ok_or(CodecError::InvalidPacket)?
                {
                    validate_event(event, context)?;
                }
            }
            "context.event" => {
                // Item sequence is a stable log identity, not the update
                // cursor. Preserve the explicit source cursor on live edits.
                safe_integer(data.get("last_sequence").ok_or(CodecError::InvalidPacket)?)?;
                let mut event = data.clone();
                event.remove("last_sequence");
                validate_event(&Value::Object(event), context)?;
            }
            _ => {
                exact_keys(&data, &["contract_version", "context_id", "status"])?;
                if !matches!(
                    data.get("status").and_then(Value::as_str),
                    Some("completed" | "canceled" | "failed" | "stopped")
                ) {
                    return Err(CodecError::InvalidPacket);
                }
            }
        }
        if data
            .get("contract_version")
            .is_some_and(|value| value.as_u64() != Some(1))
        {
            return Err(CodecError::InvalidPacket);
        }
        data.insert("contract_version".into(), Value::Number("1".into()));
        let bytes = RpcMessage::Request(RpcRequest {
            id: None,
            method: method.into(),
            params: Value::Object(data),
        })
        .encode();
        rpc::parse_message(&bytes, Peer::Server).map_err(|_| CodecError::InvalidPacket)?;
        Ok(Some(bytes))
    }

    pub(crate) fn forget(&mut self, native_id: &str) {
        if let Some(ack) = self
            .pending
            .iter()
            .find_map(|(ack, pending)| (pending.native_id == native_id).then_some(*ack))
        {
            self.pending.remove(&ack);
            self.remember(ack);
        }
    }

    fn remember(&mut self, ack: u32) {
        self.completed.push_back(ack);
        if self.completed.len() > MAX_COMPLETED {
            self.completed.pop_front();
        }
    }
}

fn exact_keys(fields: &BTreeMap<String, Value>, keys: &[&str]) -> Result<(), CodecError> {
    if fields.keys().any(|key| !keys.contains(&key.as_str())) {
        return Err(CodecError::InvalidPacket);
    }
    Ok(())
}
fn id<'a>(fields: &'a BTreeMap<String, Value>, key: &str) -> Result<&'a str, CodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| rpc::valid_opaque_id(value))
        .ok_or(CodecError::InvalidPacket)
}
fn bounded_text<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    max: usize,
) -> Result<&'a str, CodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= max)
        .ok_or(CodecError::InvalidPacket)
}
fn safe_integer(value: &Value) -> Result<u64, CodecError> {
    value
        .as_u64()
        .filter(|v| *v <= 9_007_199_254_740_991)
        .ok_or(CodecError::InvalidPacket)
}
fn validate_credential_status(data: &BTreeMap<String, Value>) -> Result<(), CodecError> {
    exact_keys(
        data,
        &[
            "contract_version",
            "action",
            "rotation_id",
            "key_generation",
            "status",
            "expires_at_ms",
        ],
    )?;
    if data.len() != 6
        || data.get("contract_version").and_then(Value::as_u64) != Some(1)
        || data
            .get("key_generation")
            .and_then(Value::as_u64)
            .is_none_or(|n| !(1..=2_147_483_647).contains(&n))
    {
        return Err(CodecError::InvalidResponse);
    }
    let action = data.get("action").and_then(Value::as_str);
    let status = data.get("status").and_then(Value::as_str);
    if action == Some("revoke") {
        if status != Some("revoked")
            || data.get("rotation_id") != Some(&Value::Null)
            || data.get("expires_at_ms") != Some(&Value::Null)
        {
            return Err(CodecError::InvalidResponse);
        }
    } else if matches!(action, Some("rotate" | "status"))
        && matches!(status, Some("pending" | "active" | "expired"))
    {
        id(data, "rotation_id")?;
        if status == Some("pending") {
            safe_integer(
                data.get("expires_at_ms")
                    .ok_or(CodecError::InvalidResponse)?,
            )?;
        } else if data.get("expires_at_ms") != Some(&Value::Null) {
            return Err(CodecError::InvalidResponse);
        }
    } else {
        return Err(CodecError::InvalidResponse);
    }
    Ok(())
}

fn validate_queue(value: &Value) -> Result<(), CodecError> {
    let items = value
        .as_array()
        .filter(|items| items.len() <= 32)
        .ok_or(CodecError::InvalidPacket)?;
    let mut ids = BTreeSet::new();
    for item in items {
        let fields = item.as_object().ok_or(CodecError::InvalidPacket)?;
        exact_keys(fields, &["id", "text", "attachments", "attachment_count"])?;
        if fields.len() != 4
            || !ids.insert(id(fields, "id")?)
            || fields
                .get("text")
                .and_then(Value::as_str)
                .is_none_or(|text| text.chars().count() > 100)
            || fields
                .get("attachments")
                .and_then(Value::as_array)
                .is_none_or(|items| !items.is_empty())
            || fields.get("attachment_count").and_then(Value::as_u64) != Some(0)
        {
            return Err(CodecError::InvalidPacket);
        }
    }
    Ok(())
}
fn validate_event(value: &Value, context: &str) -> Result<(), CodecError> {
    let fields = value.as_object().ok_or(CodecError::InvalidPacket)?;
    exact_keys(
        fields,
        &[
            "contract_version",
            "context_id",
            "sequence",
            "event",
            "data",
            "timestamp_ms",
            "correlation_id",
        ],
    )?;
    if id(fields, "context_id")? != context {
        return Err(CodecError::InvalidPacket);
    }
    safe_integer(fields.get("sequence").ok_or(CodecError::InvalidPacket)?)?;
    if let Some(value) = fields.get("timestamp_ms") {
        safe_integer(value)?;
    }
    if fields.contains_key("correlation_id") {
        id(fields, "correlation_id")?;
    }
    let data = fields
        .get("data")
        .and_then(Value::as_object)
        .ok_or(CodecError::InvalidPacket)?;
    match fields.get("event").and_then(Value::as_str) {
        Some("message") => {
            exact_keys(data, &["role", "text"])?;
            if !matches!(
                data.get("role").and_then(Value::as_str),
                Some("user" | "assistant")
            ) {
                return Err(CodecError::InvalidPacket);
            }
            bounded_text(data, "text", 8192)?;
        }
        Some("activity") => {
            exact_keys(data, &["activity", "status"])?;
            if !matches!(
                data.get("activity").and_then(Value::as_str),
                Some("assistant_work" | "browser" | "code" | "subagent" | "status" | "tool")
            ) || !matches!(
                data.get("status").and_then(Value::as_str),
                Some("working" | "updated" | "failed" | "warning")
            ) {
                return Err(CodecError::InvalidPacket);
            }
        }
        _ => return Err(CodecError::InvalidPacket),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn credential_requests_use_native_owned_material_and_exact_receipts() {
        let mut codec = ContextCodec::new();
        let fixture = json::parse(include_bytes!(
            "../tests/fixtures/credential-control-v1.json"
        ))
        .unwrap();
        for action in ["rotate", "status", "revoke"] {
            validate_credential_status(
                fixture
                    .as_object()
                    .unwrap()
                    .get(action)
                    .unwrap()
                    .as_object()
                    .unwrap()
                    .get("response")
                    .unwrap()
                    .as_object()
                    .unwrap(),
            )
            .unwrap();
        }
        let input = request("rotate-1", "credential.rotate", r#"{"contract_version":1}"#);
        let doc = json::parse(br#"{"contract_version":1,"action":"rotate","rotation_id":"rotation-1","public_key":{"algorithm":"Ed25519","encoding":"raw-base64url","value":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}"#).unwrap();
        assert!(codec.request(&input).is_err());
        assert!(codec
            .credential_request(
                &request(
                    "bad",
                    "credential.rotate",
                    r#"{"contract_version":1,"public_key":"caller-key"}"#
                ),
                doc.clone()
            )
            .is_err());
        assert!(codec
            .credential_request(&input, doc)
            .unwrap()
            .contains("connector_bridge_credential_control"));
        assert!(codec.response(&ack(2, r#"{"contract_version":1,"action":"rotate","rotation_id":"other","key_generation":1,"status":"pending","expires_at_ms":1000}"#)).is_err());
        assert!(codec.response(&ack(2, r#"{"contract_version":1,"action":"rotate","rotation_id":"rotation-1","key_generation":1,"status":"pending","expires_at_ms":1000}"#)).unwrap().is_some());
        codec
            .credential_request(
                &request("revoke-1", "credential.revoke", r#"{"contract_version":1}"#),
                json::parse(br#"{"contract_version":1,"action":"revoke"}"#).unwrap(),
            )
            .unwrap();
        let event = r#"42/ws,["connector_bridge_credential_status",{"handlerId":"ws_connector.WsConnector","data":{"contract_version":1,"action":"revoke","rotation_id":null,"key_generation":1,"status":"revoked","expires_at_ms":null}}]"#;
        let receipt = codec.notification(event).unwrap().unwrap();
        assert!(String::from_utf8(receipt)
            .unwrap()
            .contains("\"id\":\"revoke-1\""));
        assert!(codec.response(&ack(3, r#"{"contract_version":1,"action":"revoke","rotation_id":null,"key_generation":1,"status":"revoked","expires_at_ms":null}"#)).unwrap().is_none());
    }
    fn request(id: &str, method: &str, params: &str) -> Vec<u8> {
        format!(r#"{{"jsonrpc":"2.0","id":"{id}","method":"{method}","params":{params}}}"#)
            .into_bytes()
    }
    fn ack(number: u32, data: &str) -> String {
        format!(
            r#"43/ws,{number}[{{"correlationId":"native-context-{number}","results":[{{"handlerId":"ws_connector.WsConnector","correlationId":"native-context-{number}","ok":true,"data":{data}}}]}}]"#
        )
    }
    fn listed() -> ContextCodec {
        let mut codec = ContextCodec::new();
        assert!(codec
            .request(&request(
                "list-1",
                "context.list",
                r#"{"contract_version":1}"#
            ))
            .unwrap()
            .starts_with("42/ws,2["));
        codec.response(&ack(2, r#"{"contexts":[{"context_id":"ctx-1","label":"Task","kind":"chat","status":"idle","created_at_ms":1,"updated_at_ms":2}]}"#)).unwrap();
        codec
    }
    #[test]
    fn exact_list_subscribe_notification_and_ack_roundtrip() {
        let mut codec = listed();
        codec
            .request(&request(
                "subscribe-1",
                "context.subscribe",
                r#"{"contract_version":1,"context_id":"ctx-1","history":"tail"}"#,
            ))
            .unwrap();
        let packet = r#"42/ws,["connector_context_snapshot",{"handlerId":"ws_connector.WsConnector","data":{"context_id":"ctx-1","events":[{"context_id":"ctx-1","sequence":1,"event":"message","data":{"role":"assistant","text":"Hello"}}],"last_sequence":1,"complete":true,"history_before":0,"has_more_history":false}}]"#;
        let native = codec.notification(packet).unwrap().unwrap();
        assert!(String::from_utf8(native)
            .unwrap()
            .contains("context.snapshot"));
        let reply = codec
            .response(&ack(
                3,
                r#"{"context_id":"ctx-1","subscribed":true,"last_sequence":1}"#,
            ))
            .unwrap()
            .unwrap();
        assert!(String::from_utf8(reply).unwrap().contains("subscribe-1"));
        assert_eq!(codec.response(&ack(3, "{}")), Ok(None));
        let live = r#"42/ws,["connector_context_event",{"handlerId":"ws_connector.WsConnector","data":{"context_id":"ctx-1","sequence":1,"last_sequence":7,"event":"message","data":{"role":"assistant","text":"Updated"}}}]"#;
        let projected = String::from_utf8(codec.notification(live).unwrap().unwrap()).unwrap();
        assert!(projected.contains("\"last_sequence\":7"));
        assert!(codec
            .notification(&live.replace("\"last_sequence\":7,", ""))
            .is_err());
        assert!(codec
            .notification(&packet.replace(
                "\"text\":\"Hello\"",
                "\"text\":\"Hello\",\"kvps\":{\"password\":\"secret\"}"
            ))
            .is_err());
        codec
            .request(&request(
                "unsubscribe-1",
                "context.unsubscribe",
                r#"{"contract_version":1,"context_id":"ctx-1"}"#,
            ))
            .unwrap();
        assert_eq!(codec.notification(packet), Ok(None));
    }
    #[test]
    fn unsolicited_contexts_raw_artifacts_and_expired_acks_do_not_cross() {
        let mut codec = listed();
        assert!(codec
            .request(&request(
                "foreign",
                "context.subscribe",
                r#"{"contract_version":1,"context_id":"foreign"}"#
            ))
            .is_err());
        assert!(codec.request(&request("message", "context.send_message", r#"{"contract_version":1,"context_id":"ctx-1","client_message_id":"msg-1","text":"Hello","artifact_ids":["host-path"]}"#)).is_err());
        codec.request(&request("message", "context.send_message", r#"{"contract_version":1,"context_id":"ctx-1","client_message_id":"msg-1","text":"Hello"}"#)).unwrap();
        codec.forget("message");
        assert_eq!(codec.response(&ack(3, "{}")), Ok(None));
    }

    #[test]
    fn queue_and_explicit_approval_dispatch_bind_exact_results() {
        let mut codec = listed();
        let queued = codec.request(&request("queue-add", "context.queue_add", r#"{"contract_version":1,"context_id":"ctx-1","client_message_id":"message-1","text":"queued text"}"#)).unwrap();
        assert!(queued.contains("connector_message_queue_add"));
        let data = r#"{"contract_version":1,"context_id":"ctx-1","item_id":"queue-1","status":"queued","message_queue":[{"id":"queue-1","text":"queued text","attachments":[],"attachment_count":0}]}"#;
        assert!(codec
            .response(&ack(
                3,
                &data.replace("\"attachments\":[]", "\"attachments\":[\"/private/path\"]")
            ))
            .is_err());
        assert!(codec.response(&ack(3, data)).unwrap().is_some());
        assert!(codec
            .request(&request(
                "queue-send",
                "context.queue_send",
                r#"{"contract_version":1,"context_id":"ctx-1","item_id":"queue-1"}"#
            ))
            .unwrap()
            .contains("connector_message_queue_send"));
        assert!(codec
            .response(&ack(4, &data.replace("queue-1", "foreign")))
            .is_err());
        codec
            .response(&ack(
                4,
                &data.replace("\"status\":\"queued\"", "\"status\":\"sent\""),
            ))
            .unwrap();
        assert!(codec.request(&request("approve", "browser.approval_decision", r#"{"contract_version":1,"challenge_id":"challenge-1","kind":"action","decision":"approve_once"}"#)).unwrap().contains("connector_browser_approval_decision"));
        let decision = r#"{"contract_version":1,"challenge_id":"challenge-1","decision":"approved","control_id":"control-1","status":"accepted"}"#;
        assert!(codec
            .response(&ack(5, &decision.replace("challenge-1", "foreign")))
            .is_err());
        assert!(codec.response(&ack(5, decision)).unwrap().is_some());
        assert!(codec.request(&request("bad-approval", "browser.approval_decision", r#"{"contract_version":1,"challenge_id":"challenge-1","kind":"action","decision":"allow_turn"}"#)).is_err());
    }

    #[test]
    fn queue_notification_is_bounded_and_subscription_scoped() {
        let mut codec = listed();
        let packet = r#"42/ws,["connector_message_queue_updated",{"handlerId":"ws_connector.WsConnector","data":{"contract_version":1,"context_id":"ctx-1","message_queue":[{"id":"queue-1","text":"Preview","attachments":[],"attachment_count":0}]}}]"#;
        assert_eq!(codec.notification(packet), Ok(None));
        codec
            .request(&request(
                "sub",
                "context.subscribe",
                r#"{"contract_version":1,"context_id":"ctx-1"}"#,
            ))
            .unwrap();
        let forwarded = String::from_utf8(codec.notification(packet).unwrap().unwrap()).unwrap();
        assert!(forwarded.contains("context.queue_updated"));
        assert!(codec
            .notification(&packet.replace(
                "\"text\":\"Preview\"",
                "\"text\":\"Preview\",\"url\":\"https://private.example\""
            ))
            .is_err());
        assert!(codec
            .request(&request(
                "foreign",
                "context.queue_remove",
                r#"{"contract_version":1,"context_id":"foreign","item_id":"queue-1"}"#
            ))
            .is_err());
    }

    #[test]
    fn shared_core_worker_local_approval_fixture_has_exact_native_mapping() {
        let fixture = json::parse(include_bytes!(
            "../tests/fixtures/context-queue-approval-v1.json"
        ))
        .unwrap();
        let action = fixture
            .as_object()
            .unwrap()
            .get("action_approval")
            .unwrap()
            .as_object()
            .unwrap();
        let mut codec = ContextCodec::new();
        let request_bytes = request(
            "shared-approval",
            action.get("native_method").unwrap().as_str().unwrap(),
            &action.get("params").unwrap().encode(),
        );
        let packet = codec.request(&request_bytes).unwrap();
        assert!(packet.contains(action.get("connector_event").unwrap().as_str().unwrap()));
        let result = codec
            .response(&ack(2, &action.get("result").unwrap().encode()))
            .unwrap()
            .unwrap();
        let RpcMessage::Response(response) = rpc::parse_message(&result, Peer::Server).unwrap()
        else {
            panic!("expected response")
        };
        assert_eq!(response.result.unwrap(), *action.get("result").unwrap());
    }
}
