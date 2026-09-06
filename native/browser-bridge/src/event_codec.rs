//! Bounded critical browser-event transport.
//!
//! Extension events are notification-only. They enter the retained set only
//! after the owning relay has queued the generated Socket.IO event to its
//! authenticated Core connection. Core acknowledgements are generation- and
//! credential-bound commands; retained events are released only after the
//! extension returns the exact `browser.ack_events` echo.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use sha2::{Digest, Sha256};

use crate::json::{self, Value};
use crate::rpc::{self, Peer, RpcMessage, RpcRequest, RpcResponse};
use crate::transport_profile::BrowserTransportProfile;
#[cfg(test)]
use crate::transport_profile::PRODUCTION_HANDLER_ID as HANDLER_ID;

pub(crate) const MAX_RETAINED_EVENTS: usize = 1_024;
pub(crate) const MAX_RETAINED_BYTES: usize = 8 * 1024 * 1024;
pub(crate) const MAX_PENDING_ACKS: usize = 64;
pub(crate) const MAX_COMPLETED_IDS: usize = 2_048;
const ACK_TIMEOUT_MS: u64 = rpc::MAX_REQUEST_TIMEOUT_MS;
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

const EVENT_KEYS: &[&str] = &[
    "contract_version",
    "event_id",
    "load_generation_id",
    "event_sequence",
    "delivery",
    "event_type",
    "observed_at_ms",
    "context_id",
    "browser_session_id",
    "turn_id",
    "op_id",
    "action_id",
    "data",
];

const LEASE_DATA_KEYS: &[&str] = &[
    "lease_id_digest",
    "browser_id_digest",
    "state",
    "ownership",
    "disposition",
    "change",
    "reason_code",
];

const FINALIZED_DATA_KEYS: &[&str] = &[
    "control_id",
    "status",
    "closed_count",
    "released_count",
    "retained_count",
    "already_finalized_count",
    "error_count",
];

const SITE_CHALLENGE_DATA_KEYS: &[&str] = &[
    "challenge_id",
    "kind",
    "origin",
    "action_class",
    "canonical_parameter_hash",
    "target_fingerprint",
    "lease_id_digest",
    "browser_id_digest",
    "document_id",
    "document_epoch",
    "summary",
    "options",
    "expires_at_ms",
];

const ACTION_CHALLENGE_DATA_KEYS: &[&str] = &[
    "challenge_id",
    "kind",
    "origin",
    "action_class",
    "canonical_parameter_hash",
    "target_fingerprint",
    "lease_id_digest",
    "browser_id_digest",
    "document_id",
    "document_epoch",
    "summary",
    "options",
    "data_classification",
    "expires_at_ms",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum EventCodecError {
    InvalidPacket,
    UnsupportedEvent,
    InvalidResponse,
    UnknownResponse,
    Capacity,
    ClockInvalid,
}

#[derive(Clone)]
struct EventIdentity {
    event_id: String,
    sequence: u64,
    canonical_digest: [u8; 32],
}

#[derive(Clone)]
struct RetainedEvent {
    identity: EventIdentity,
    encoded_bytes: usize,
}

struct PendingAck {
    native_id: String,
    correlation: String,
    bridge_id: String,
    highest_sequence: u64,
    deadline_ms: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct AckIdentity {
    bridge_id: String,
    highest_sequence: u64,
}

pub(crate) struct PreparedEvent {
    packet: Option<String>,
    record: Option<RetainedEvent>,
}

impl PreparedEvent {
    pub(crate) fn packet(&self) -> Option<&str> {
        self.packet.as_deref()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum AckResponseDisposition {
    NotOwned,
    Handled,
}

pub(crate) struct EventCodec {
    transport_profile: BrowserTransportProfile,
    load_generation_id: String,
    highest_committed_sequence: u64,
    highest_acknowledged_sequence: u64,
    event_correlation_sequence: u64,
    ack_request_sequence: u64,
    retained_bytes: usize,
    retained_by_id: BTreeMap<String, RetainedEvent>,
    retained_by_sequence: BTreeMap<u64, String>,
    completed_events: BTreeMap<String, EventIdentity>,
    completed_event_sequences: BTreeMap<u64, String>,
    completed_event_order: VecDeque<String>,
    pending_acks: BTreeMap<String, PendingAck>,
    ack_correlations: BTreeMap<String, AckIdentity>,
    completed_ack_correlations: VecDeque<String>,
    completed_ack_ids: BTreeSet<String>,
    completed_ack_id_order: VecDeque<String>,
}

impl EventCodec {
    pub(crate) fn new(load_generation_id: String, last_acknowledged_sequence: u64) -> Self {
        Self::with_profile(
            load_generation_id,
            last_acknowledged_sequence,
            BrowserTransportProfile::compiled(),
        )
    }

    pub(crate) fn with_profile(
        load_generation_id: String,
        last_acknowledged_sequence: u64,
        transport_profile: BrowserTransportProfile,
    ) -> Self {
        Self {
            transport_profile,
            load_generation_id,
            highest_committed_sequence: last_acknowledged_sequence,
            highest_acknowledged_sequence: last_acknowledged_sequence,
            event_correlation_sequence: 0,
            ack_request_sequence: 0,
            retained_bytes: 0,
            retained_by_id: BTreeMap::new(),
            retained_by_sequence: BTreeMap::new(),
            completed_events: BTreeMap::new(),
            completed_event_sequences: BTreeMap::new(),
            completed_event_order: VecDeque::new(),
            pending_acks: BTreeMap::new(),
            ack_correlations: BTreeMap::new(),
            completed_ack_correlations: VecDeque::new(),
            completed_ack_ids: BTreeSet::new(),
            completed_ack_id_order: VecDeque::new(),
        }
    }

    /// Validate and prepare one extension notification without claiming it was
    /// queued. `commit_event` must be called only after Core queue success.
    pub(crate) fn prepare_event(
        &mut self,
        request: &RpcRequest,
    ) -> Result<PreparedEvent, EventCodecError> {
        if request.method != "browser.event" || request.id.is_some() {
            return Err(EventCodecError::UnsupportedEvent);
        }
        let fields = validate_event_params(
            &request.params,
            &self.load_generation_id,
            self.transport_profile,
        )?;
        let event_id = identifier(fields, "event_id")?.to_owned();
        let sequence = integer(fields, "event_sequence")?;
        let canonical = request.params.encode();
        let canonical_digest: [u8; 32] = Sha256::digest(canonical.as_bytes()).into();
        let identity = EventIdentity {
            event_id: event_id.clone(),
            sequence,
            canonical_digest,
        };

        if let Some(existing) = self.retained_by_id.get(&event_id) {
            return if same_event(&existing.identity, &identity) {
                Ok(PreparedEvent {
                    packet: None,
                    record: None,
                })
            } else {
                Err(EventCodecError::InvalidPacket)
            };
        }
        if let Some(existing) = self.completed_events.get(&event_id) {
            return if same_event(existing, &identity) {
                Ok(PreparedEvent {
                    packet: None,
                    record: None,
                })
            } else {
                Err(EventCodecError::InvalidPacket)
            };
        }
        if self
            .retained_by_sequence
            .get(&sequence)
            .or_else(|| self.completed_event_sequences.get(&sequence))
            .is_some()
        {
            return Err(EventCodecError::InvalidPacket);
        }
        if sequence
            != self
                .highest_committed_sequence
                .checked_add(1)
                .ok_or(EventCodecError::Capacity)?
        {
            return Err(EventCodecError::InvalidPacket);
        }
        let encoded_bytes = canonical.len();
        if self.retained_by_id.len() >= MAX_RETAINED_EVENTS
            || self
                .retained_bytes
                .checked_add(encoded_bytes)
                .is_none_or(|total| total > MAX_RETAINED_BYTES)
        {
            return Err(EventCodecError::Capacity);
        }

        self.event_correlation_sequence = self
            .event_correlation_sequence
            .checked_add(1)
            .ok_or(EventCodecError::Capacity)?;
        let correlation = format!("native_event_{}", self.event_correlation_sequence);
        let packet = format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_event".into()),
                Value::Object(BTreeMap::from([
                    ("correlationId".into(), Value::String(correlation)),
                    ("data".into(), request.params.clone()),
                ])),
            ])
            .encode()
        );
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(EventCodecError::InvalidPacket);
        }
        Ok(PreparedEvent {
            packet: Some(packet),
            record: Some(RetainedEvent {
                identity,
                encoded_bytes,
            }),
        })
    }

    pub(crate) fn commit_event(&mut self, prepared: PreparedEvent) -> Result<(), EventCodecError> {
        let Some(record) = prepared.record else {
            return Ok(());
        };
        if prepared.packet.is_none()
            || record.identity.sequence
                != self
                    .highest_committed_sequence
                    .checked_add(1)
                    .ok_or(EventCodecError::Capacity)?
            || self.retained_by_id.len() >= MAX_RETAINED_EVENTS
            || self
                .retained_bytes
                .checked_add(record.encoded_bytes)
                .is_none_or(|total| total > MAX_RETAINED_BYTES)
            || self.retained_by_id.contains_key(&record.identity.event_id)
            || self
                .retained_by_sequence
                .contains_key(&record.identity.sequence)
        {
            return Err(EventCodecError::InvalidPacket);
        }
        self.retained_bytes += record.encoded_bytes;
        self.highest_committed_sequence = record.identity.sequence;
        self.retained_by_sequence
            .insert(record.identity.sequence, record.identity.event_id.clone());
        self.retained_by_id
            .insert(record.identity.event_id.clone(), record);
        Ok(())
    }

    /// Map an authenticated Core event ACK to one native request. The Core
    /// packet's `bridge_id` is checked against the credential-bound value
    /// supplied separately by `CoreConnection`.
    pub(crate) fn ack_command(
        &mut self,
        packet: &str,
        credential_bridge_id: &str,
        now_ms: u64,
    ) -> Result<Option<Vec<u8>>, EventCodecError> {
        if packet.len() > rpc::MAX_NON_ARTIFACT_BYTES {
            return Err(EventCodecError::InvalidPacket);
        }
        let array = json::parse(
            packet
                .strip_prefix("42/ws,")
                .ok_or(EventCodecError::InvalidPacket)?
                .as_bytes(),
        )
        .map_err(|_| EventCodecError::InvalidPacket)?;
        let array = array
            .as_array()
            .filter(|values| values.len() == 2)
            .ok_or(EventCodecError::InvalidPacket)?;
        let event = array[0].as_str().ok_or(EventCodecError::InvalidPacket)?;
        if event != "connector_browser_event_ack" {
            return Err(EventCodecError::UnsupportedEvent);
        }
        let envelope = array[1].as_object().ok_or(EventCodecError::InvalidPacket)?;
        validate_ack_envelope(envelope)?;
        if envelope.get("handlerId").and_then(Value::as_str)
            != Some(self.transport_profile.handler_id())
        {
            return Err(EventCodecError::InvalidPacket);
        }
        let correlation = core_correlation(envelope, "correlationId")?.to_owned();
        let data = envelope
            .get("data")
            .and_then(Value::as_object)
            .ok_or(EventCodecError::InvalidPacket)?;
        exact_keys(
            data,
            &[
                "contract_version",
                "bridge_id",
                "load_generation_id",
                "highest_contiguous_event_sequence",
            ],
        )?;
        if integer(data, "contract_version")? != 1
            || identifier(data, "bridge_id")? != credential_bridge_id
            || identifier(data, "load_generation_id")? != self.load_generation_id
        {
            return Err(EventCodecError::InvalidPacket);
        }
        let highest_sequence = integer(data, "highest_contiguous_event_sequence")?;
        if highest_sequence > self.highest_committed_sequence
            || highest_sequence < self.highest_acknowledged_sequence
        {
            return Err(EventCodecError::InvalidPacket);
        }
        let ack_identity = AckIdentity {
            bridge_id: credential_bridge_id.to_owned(),
            highest_sequence,
        };
        if let Some(existing) = self.ack_correlations.get(&correlation) {
            return if *existing == ack_identity {
                Ok(None)
            } else {
                Err(EventCodecError::InvalidPacket)
            };
        }
        if highest_sequence == self.highest_acknowledged_sequence {
            self.remember_ack_correlation(correlation, ack_identity);
            return Ok(None);
        }
        if self.pending_acks.len() >= MAX_PENDING_ACKS {
            return Err(EventCodecError::Capacity);
        }
        let deadline_ms = now_ms
            .checked_add(ACK_TIMEOUT_MS)
            .ok_or(EventCodecError::ClockInvalid)?;
        self.ack_request_sequence = self
            .ack_request_sequence
            .checked_add(1)
            .ok_or(EventCodecError::Capacity)?;
        let native_id = format!("core-event-ack-{}", self.ack_request_sequence);
        let params = Value::Object(BTreeMap::from([
            ("contract_version".into(), Value::Number("1".into())),
            (
                "load_generation_id".into(),
                Value::String(self.load_generation_id.clone()),
            ),
            (
                "highest_contiguous_event_sequence".into(),
                Value::Number(highest_sequence.to_string()),
            ),
        ]));
        let message = RpcMessage::Request(RpcRequest {
            id: Some(native_id.clone()),
            method: "browser.ack_events".into(),
            params,
        })
        .encode();
        rpc::parse_message(&message, Peer::Server).map_err(|_| EventCodecError::InvalidPacket)?;
        self.ack_correlations
            .insert(correlation.clone(), ack_identity);
        self.pending_acks.insert(
            native_id.clone(),
            PendingAck {
                native_id,
                correlation,
                bridge_id: credential_bridge_id.to_owned(),
                highest_sequence,
                deadline_ms,
            },
        );
        Ok(Some(message))
    }

    /// Settle only IDs allocated by this codec. Non-owned response IDs are
    /// returned to the ordinary operation/control correlation path.
    pub(crate) fn ack_response(
        &mut self,
        response: &RpcResponse,
        now_ms: u64,
    ) -> Result<AckResponseDisposition, EventCodecError> {
        if self.completed_ack_ids.contains(&response.id) {
            return Ok(AckResponseDisposition::Handled);
        }
        let Some(pending) = self.pending_acks.get(&response.id) else {
            return if response.id.starts_with("core-event-ack-") {
                Err(EventCodecError::UnknownResponse)
            } else {
                Ok(AckResponseDisposition::NotOwned)
            };
        };
        if now_ms >= pending.deadline_ms {
            let native_id = response.id.clone();
            self.complete_ack_id(&native_id);
            return Ok(AckResponseDisposition::Handled);
        }
        validate_ack_response(response, pending, &self.load_generation_id)?;
        let highest_sequence = pending.highest_sequence;
        self.highest_acknowledged_sequence =
            self.highest_acknowledged_sequence.max(highest_sequence);
        self.release_retained_through(highest_sequence);

        let completed = self
            .pending_acks
            .values()
            .filter(|candidate| candidate.highest_sequence <= highest_sequence)
            .map(|candidate| candidate.native_id.clone())
            .collect::<Vec<_>>();
        for native_id in completed {
            self.complete_ack_id(&native_id);
        }
        Ok(AckResponseDisposition::Handled)
    }

    pub(crate) fn expire(&mut self, now_ms: u64) {
        let expired = self
            .pending_acks
            .values()
            .filter(|pending| now_ms >= pending.deadline_ms)
            .map(|pending| pending.native_id.clone())
            .collect::<Vec<_>>();
        for native_id in expired {
            self.complete_ack_id(&native_id);
        }
    }

    #[cfg(test)]
    fn retained_count(&self) -> usize {
        self.retained_by_id.len()
    }

    #[cfg(test)]
    fn pending_ack_count(&self) -> usize {
        self.pending_acks.len()
    }

    fn release_retained_through(&mut self, highest_sequence: u64) {
        let sequences = self
            .retained_by_sequence
            .range(..=highest_sequence)
            .map(|(sequence, _)| *sequence)
            .collect::<Vec<_>>();
        for sequence in sequences {
            let Some(event_id) = self.retained_by_sequence.remove(&sequence) else {
                continue;
            };
            let Some(event) = self.retained_by_id.remove(&event_id) else {
                continue;
            };
            self.retained_bytes = self.retained_bytes.saturating_sub(event.encoded_bytes);
            self.remember_completed_event(event.identity);
        }
    }

    fn remember_completed_event(&mut self, identity: EventIdentity) {
        let event_id = identity.event_id.clone();
        self.completed_event_sequences
            .insert(identity.sequence, event_id.clone());
        self.completed_events.insert(event_id.clone(), identity);
        self.completed_event_order.push_back(event_id);
        while self.completed_event_order.len() > MAX_COMPLETED_IDS {
            let Some(oldest) = self.completed_event_order.pop_front() else {
                break;
            };
            if let Some(removed) = self.completed_events.remove(&oldest) {
                if self.completed_event_sequences.get(&removed.sequence) == Some(&oldest) {
                    self.completed_event_sequences.remove(&removed.sequence);
                }
            }
        }
    }

    fn remember_ack_correlation(&mut self, correlation: String, identity: AckIdentity) {
        self.ack_correlations.insert(correlation.clone(), identity);
        self.completed_ack_correlations.push_back(correlation);
        self.trim_completed_ack_correlations();
    }

    fn complete_ack_id(&mut self, native_id: &str) {
        let Some(pending) = self.pending_acks.remove(native_id) else {
            return;
        };
        debug_assert_eq!(pending.native_id, native_id);
        debug_assert_eq!(
            self.ack_correlations.get(&pending.correlation),
            Some(&AckIdentity {
                bridge_id: pending.bridge_id,
                highest_sequence: pending.highest_sequence,
            })
        );
        self.completed_ack_correlations
            .push_back(pending.correlation);
        self.trim_completed_ack_correlations();
        if self.completed_ack_ids.insert(native_id.to_owned()) {
            self.completed_ack_id_order.push_back(native_id.to_owned());
        }
        while self.completed_ack_id_order.len() > MAX_COMPLETED_IDS {
            if let Some(oldest) = self.completed_ack_id_order.pop_front() {
                self.completed_ack_ids.remove(&oldest);
            }
        }
    }

    fn trim_completed_ack_correlations(&mut self) {
        while self.completed_ack_correlations.len() > MAX_COMPLETED_IDS {
            if let Some(oldest) = self.completed_ack_correlations.pop_front() {
                if !self
                    .pending_acks
                    .values()
                    .any(|pending| pending.correlation == oldest)
                {
                    self.ack_correlations.remove(&oldest);
                }
            }
        }
    }
}

fn validate_event_params<'a>(
    params: &'a Value,
    load_generation_id: &str,
    transport_profile: BrowserTransportProfile,
) -> Result<&'a BTreeMap<String, Value>, EventCodecError> {
    let fields = params.as_object().ok_or(EventCodecError::InvalidPacket)?;
    exact_keys(fields, EVENT_KEYS)?;
    let sequence = integer(fields, "event_sequence")?;
    let observed_at_ms = integer(fields, "observed_at_ms")?;
    if integer(fields, "contract_version")? != 1
        || identifier(fields, "load_generation_id")? != load_generation_id
        || fields.get("delivery").and_then(Value::as_str) != Some("critical")
        || sequence == 0
        || sequence > MAX_SAFE_INTEGER
        || observed_at_ms > MAX_SAFE_INTEGER
    {
        return Err(EventCodecError::InvalidPacket);
    }
    for key in ["event_id", "context_id", "browser_session_id", "turn_id"] {
        identifier(fields, key)?;
    }
    for key in ["op_id", "action_id"] {
        if !matches!(fields.get(key), Some(Value::Null)) {
            identifier(fields, key)?;
        }
    }
    let data = fields
        .get("data")
        .and_then(Value::as_object)
        .ok_or(EventCodecError::InvalidPacket)?;
    match fields.get("event_type").and_then(Value::as_str) {
        Some("lease.changed") => validate_lease_data(data)?,
        Some("turn.finalized") => validate_finalized_data(data)?,
        Some("challenge.required") => {
            identifier(fields, "op_id")?;
            identifier(fields, "action_id")?;
            if !transport_profile.permits_action_challenge()
                && data.get("kind").and_then(Value::as_str) == Some("action")
            {
                return Err(EventCodecError::UnsupportedEvent);
            }
            validate_challenge_data(data)?;
        }
        _ => return Err(EventCodecError::UnsupportedEvent),
    }
    if params.encode().len() > rpc::MAX_EVENT_METADATA_BYTES {
        return Err(EventCodecError::InvalidPacket);
    }
    Ok(fields)
}

fn validate_lease_data(fields: &BTreeMap<String, Value>) -> Result<(), EventCodecError> {
    exact_keys(fields, LEASE_DATA_KEYS)?;
    for key in ["lease_id_digest", "browser_id_digest"] {
        if !fields
            .get(key)
            .and_then(Value::as_str)
            .is_some_and(valid_sha256)
        {
            return Err(EventCodecError::InvalidPacket);
        }
    }
    enum_value(
        fields,
        "state",
        &[
            "active",
            "finalizing",
            "closed",
            "released",
            "retained",
            "outcome_unknown",
            "orphan",
        ],
    )?;
    enum_value(fields, "ownership", &["created", "claimed"])?;
    enum_value(
        fields,
        "disposition",
        &["ephemeral", "deliverable", "handoff"],
    )?;
    let change = enum_value(
        fields,
        "change",
        &[
            "created",
            "finalizing",
            "tab_closed",
            "user_takeover",
            "finalized",
            "orphaned",
        ],
    )?;
    let reason = match fields.get("reason_code") {
        Some(Value::Null) => None,
        Some(Value::String(value))
            if [
                "FINALIZATION_STARTED",
                "TAB_CLOSED",
                "USER_TAKEOVER",
                "LEASE_ORPHANED",
                "FINALIZED_CLOSED",
                "FINALIZED_RELEASED",
                "FINALIZED_RETAINED",
                "FINALIZATION_OUTCOME_UNKNOWN",
            ]
            .contains(&value.as_str()) =>
        {
            Some(value.as_str())
        }
        _ => return Err(EventCodecError::InvalidPacket),
    };
    if (change == "created") != reason.is_none() {
        return Err(EventCodecError::InvalidPacket);
    }
    Ok(())
}

fn validate_finalized_data(fields: &BTreeMap<String, Value>) -> Result<(), EventCodecError> {
    exact_keys(fields, FINALIZED_DATA_KEYS)?;
    identifier(fields, "control_id")?;
    if fields.get("status").and_then(Value::as_str) != Some("completed") {
        return Err(EventCodecError::InvalidPacket);
    }
    for key in [
        "closed_count",
        "released_count",
        "retained_count",
        "already_finalized_count",
        "error_count",
    ] {
        if integer(fields, key)? > 256 {
            return Err(EventCodecError::InvalidPacket);
        }
    }
    Ok(())
}

fn validate_challenge_data(fields: &BTreeMap<String, Value>) -> Result<(), EventCodecError> {
    let kind = fields
        .get("kind")
        .and_then(Value::as_str)
        .ok_or(EventCodecError::InvalidPacket)?;
    exact_keys(
        fields,
        match kind {
            "site" => SITE_CHALLENGE_DATA_KEYS,
            "action" => ACTION_CHALLENGE_DATA_KEYS,
            _ => return Err(EventCodecError::InvalidPacket),
        },
    )?;
    identifier(fields, "challenge_id")?;
    if !fields
        .get("origin")
        .and_then(Value::as_str)
        .is_some_and(rpc::valid_http_origin)
    {
        return Err(EventCodecError::InvalidPacket);
    }
    for key in [
        "canonical_parameter_hash",
        "target_fingerprint",
        "lease_id_digest",
        "browser_id_digest",
    ] {
        if !fields
            .get(key)
            .and_then(Value::as_str)
            .is_some_and(valid_sha256)
        {
            return Err(EventCodecError::InvalidPacket);
        }
    }
    let document_id_is_null = matches!(fields.get("document_id"), Some(Value::Null));
    if !document_id_is_null {
        identifier(fields, "document_id")?;
    }
    if integer(fields, "document_epoch")? > MAX_SAFE_INTEGER {
        return Err(EventCodecError::InvalidPacket);
    }
    let summary = fields
        .get("summary")
        .and_then(Value::as_str)
        .ok_or(EventCodecError::InvalidPacket)?;
    if summary.len() > 512 || !summary.bytes().all(|byte| matches!(byte, 0x20..=0x7e)) {
        return Err(EventCodecError::InvalidPacket);
    }
    let options = fields
        .get("options")
        .and_then(Value::as_array)
        .ok_or(EventCodecError::InvalidPacket)?;
    match kind {
        "site" => {
            if fields.get("action_class").and_then(Value::as_str) != Some("navigate")
                || options.len() != 3
                || options[0].as_str() != Some("deny")
                || options[1].as_str() != Some("allow_once")
                || options[2].as_str() != Some("allow_turn")
            {
                return Err(EventCodecError::InvalidPacket);
            }
        }
        "action" => {
            let sensitive_text = validate_action_data_classification(
                fields
                    .get("data_classification")
                    .ok_or(EventCodecError::InvalidPacket)?,
            )?;
            if summary.is_empty()
                || document_id_is_null
                || !fields
                    .get("action_class")
                    .and_then(Value::as_str)
                    .is_some_and(|value| {
                        matches!(
                            value,
                            "sensitive_input" | "external_side_effect" | "unknown"
                        )
                    })
                || (sensitive_text
                    && (fields.get("action_class").and_then(Value::as_str)
                        != Some("sensitive_input")
                        || summary != "Allow Agent Zero to type into the highlighted field?"))
                || options.len() != 2
                || options[0].as_str() != Some("decline")
                || options[1].as_str() != Some("approve_once")
            {
                return Err(EventCodecError::InvalidPacket);
            }
        }
        _ => return Err(EventCodecError::InvalidPacket),
    }
    let expires_at_ms = integer(fields, "expires_at_ms")?;
    if expires_at_ms == 0 || expires_at_ms > MAX_SAFE_INTEGER {
        return Err(EventCodecError::InvalidPacket);
    }
    Ok(())
}

fn validate_action_data_classification(value: &Value) -> Result<bool, EventCodecError> {
    if value.as_str() == Some("none") {
        return Ok(false);
    }
    let fields = value.as_object().ok_or(EventCodecError::InvalidPacket)?;
    const KEYS: &[&str] = &["kind", "sensitivity", "text_sha256"];
    if fields.len() != KEYS.len()
        || KEYS.iter().any(|key| !fields.contains_key(*key))
        || fields.get("kind").and_then(Value::as_str) != Some("text")
        || fields.get("sensitivity").and_then(Value::as_str) != Some("sensitive")
        || !fields
            .get("text_sha256")
            .and_then(Value::as_str)
            .is_some_and(valid_sha256)
    {
        return Err(EventCodecError::InvalidPacket);
    }
    Ok(true)
}

fn validate_ack_envelope(fields: &BTreeMap<String, Value>) -> Result<(), EventCodecError> {
    if !fields.contains_key("handlerId")
        || !fields.contains_key("correlationId")
        || !fields.contains_key("data")
        || fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "handlerId" | "eventId" | "correlationId" | "ts" | "data"
            )
        })
    {
        return Err(EventCodecError::InvalidPacket);
    }
    if let Some(event_id) = fields.get("eventId") {
        if !event_id.as_str().is_some_and(rpc::valid_opaque_id) {
            return Err(EventCodecError::InvalidPacket);
        }
    }
    if let Some(timestamp) = fields.get("ts") {
        if !timestamp.as_str().is_some_and(|value| {
            !value.is_empty() && value.len() <= 64 && !value.chars().any(char::is_control)
        }) {
            return Err(EventCodecError::InvalidPacket);
        }
    }
    Ok(())
}

fn validate_ack_response(
    response: &RpcResponse,
    pending: &PendingAck,
    load_generation_id: &str,
) -> Result<(), EventCodecError> {
    let result = response
        .result
        .as_ref()
        .map_err(|_| EventCodecError::InvalidResponse)?;
    let fields = result.as_object().ok_or(EventCodecError::InvalidResponse)?;
    exact_keys_response(
        fields,
        &[
            "contract_version",
            "load_generation_id",
            "highest_contiguous_event_sequence",
            "status",
        ],
    )?;
    if integer_response(fields, "contract_version")? != 1
        || fields.get("load_generation_id").and_then(Value::as_str) != Some(load_generation_id)
        || integer_response(fields, "highest_contiguous_event_sequence")?
            != pending.highest_sequence
        || fields.get("status").and_then(Value::as_str) != Some("acknowledged")
    {
        return Err(EventCodecError::InvalidResponse);
    }
    Ok(())
}

fn same_event(left: &EventIdentity, right: &EventIdentity) -> bool {
    left.event_id == right.event_id
        && left.sequence == right.sequence
        && left.canonical_digest == right.canonical_digest
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn exact_keys(fields: &BTreeMap<String, Value>, expected: &[&str]) -> Result<(), EventCodecError> {
    if fields.len() != expected.len() || expected.iter().any(|key| !fields.contains_key(*key)) {
        return Err(EventCodecError::InvalidPacket);
    }
    Ok(())
}

fn exact_keys_response(
    fields: &BTreeMap<String, Value>,
    expected: &[&str],
) -> Result<(), EventCodecError> {
    exact_keys(fields, expected).map_err(|_| EventCodecError::InvalidResponse)
}

fn identifier<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a str, EventCodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| rpc::valid_opaque_id(value))
        .ok_or(EventCodecError::InvalidPacket)
}

fn integer(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, EventCodecError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(EventCodecError::InvalidPacket)
}

fn integer_response(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, EventCodecError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(EventCodecError::InvalidResponse)
}

fn enum_value<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    allowed: &[&str],
) -> Result<&'a str, EventCodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| allowed.contains(value))
        .ok_or(EventCodecError::InvalidPacket)
}

fn core_correlation<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a str, EventCodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        })
        .ok_or(EventCodecError::InvalidPacket)
}

#[cfg(test)]
mod tests {
    use super::*;

    const GENERATION: &str = "a0g1.0123456789abcdef0123456789abcdef";
    const BRIDGE: &str = "bridge_1";
    const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn lease_event(event_id: &str, sequence: u64) -> RpcRequest {
        let payload = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"method\":\"browser.event\",\"params\":{{",
                "\"contract_version\":1,\"event_id\":\"{event_id}\",",
                "\"load_generation_id\":\"{GENERATION}\",\"event_sequence\":{sequence},",
                "\"delivery\":\"critical\",\"event_type\":\"lease.changed\",",
                "\"observed_at_ms\":1000,\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":null,\"action_id\":null,\"data\":{{",
                "\"lease_id_digest\":\"{DIGEST}\",\"browser_id_digest\":\"{DIGEST}\",",
                "\"state\":\"active\",\"ownership\":\"created\",",
                "\"disposition\":\"ephemeral\",\"change\":\"created\",\"reason_code\":null",
                "}}}}}}"
            ),
            event_id = event_id,
            GENERATION = GENERATION,
            sequence = sequence,
            DIGEST = DIGEST,
        );
        let RpcMessage::Request(request) =
            rpc::parse_message(payload.as_bytes(), Peer::Extension).unwrap()
        else {
            panic!()
        };
        request
    }

    fn finalized_event(event_id: &str, sequence: u64) -> RpcRequest {
        let payload = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"method\":\"browser.event\",\"params\":{{",
                "\"contract_version\":1,\"event_id\":\"{event_id}\",",
                "\"load_generation_id\":\"{GENERATION}\",\"event_sequence\":{sequence},",
                "\"delivery\":\"critical\",\"event_type\":\"turn.finalized\",",
                "\"observed_at_ms\":1001,\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":\"op-1\",\"action_id\":\"action-1\",\"data\":{{",
                "\"control_id\":\"control-1\",\"status\":\"completed\",",
                "\"closed_count\":1,\"released_count\":2,\"retained_count\":3,",
                "\"already_finalized_count\":4,\"error_count\":0",
                "}}}}}}"
            ),
            event_id = event_id,
            GENERATION = GENERATION,
            sequence = sequence,
        );
        let RpcMessage::Request(request) =
            rpc::parse_message(payload.as_bytes(), Peer::Extension).unwrap()
        else {
            panic!()
        };
        request
    }

    fn challenge_event(event_id: &str, sequence: u64) -> RpcRequest {
        let payload = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"method\":\"browser.event\",\"params\":{{",
                "\"contract_version\":1,\"event_id\":\"{event_id}\",",
                "\"load_generation_id\":\"{GENERATION}\",\"event_sequence\":{sequence},",
                "\"delivery\":\"critical\",\"event_type\":\"challenge.required\",",
                "\"observed_at_ms\":1001,\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":\"op-1\",\"action_id\":\"action-1\",\"data\":{{",
                "\"challenge_id\":\"challenge-1\",\"kind\":\"site\",",
                "\"origin\":\"https://example.test\",\"action_class\":\"navigate\",",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",",
                "\"lease_id_digest\":\"{DIGEST}\",\"browser_id_digest\":\"{DIGEST}\",",
                "\"document_id\":null,\"document_epoch\":0,",
                "\"summary\":\"Allow Agent Zero to work on example.test?\",",
                "\"options\":[\"deny\",\"allow_once\",\"allow_turn\"],",
                "\"expires_at_ms\":2000",
                "}}}}}}"
            ),
            event_id = event_id,
            GENERATION = GENERATION,
            sequence = sequence,
            DIGEST = DIGEST,
        );
        let RpcMessage::Request(request) =
            rpc::parse_message(payload.as_bytes(), Peer::Extension).unwrap()
        else {
            panic!()
        };
        request
    }

    fn action_challenge_event(event_id: &str, sequence: u64) -> RpcRequest {
        let payload = format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"method\":\"browser.event\",\"params\":{{",
                "\"contract_version\":1,\"event_id\":\"{event_id}\",",
                "\"load_generation_id\":\"{GENERATION}\",\"event_sequence\":{sequence},",
                "\"delivery\":\"critical\",\"event_type\":\"challenge.required\",",
                "\"observed_at_ms\":1001,\"context_id\":\"context-1\",",
                "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
                "\"op_id\":\"op-1\",\"action_id\":\"action-1\",\"data\":{{",
                "\"challenge_id\":\"challenge-1\",\"kind\":\"action\",",
                "\"origin\":\"https://example.test\",\"action_class\":\"unknown\",",
                "\"canonical_parameter_hash\":\"{DIGEST}\",",
                "\"target_fingerprint\":\"{DIGEST}\",",
                "\"lease_id_digest\":\"{DIGEST}\",\"browser_id_digest\":\"{DIGEST}\",",
                "\"document_id\":\"document-1\",\"document_epoch\":1,",
                "\"summary\":\"Approve this browser action?\",",
                "\"options\":[\"decline\",\"approve_once\"],",
                "\"data_classification\":\"none\",\"expires_at_ms\":2000",
                "}}}}}}"
            ),
            event_id = event_id,
            GENERATION = GENERATION,
            sequence = sequence,
            DIGEST = DIGEST,
        );
        let RpcMessage::Request(request) =
            rpc::parse_message(payload.as_bytes(), Peer::Extension).unwrap()
        else {
            panic!()
        };
        request
    }

    fn type_challenge_event(event_id: &str, sequence: u64) -> RpcRequest {
        let mut request = action_challenge_event(event_id, sequence);
        let Value::Object(fields) = &mut request.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "action_class".into(),
            Value::String("sensitive_input".into()),
        );
        data.insert(
            "data_classification".into(),
            Value::Object(BTreeMap::from([
                ("kind".into(), Value::String("text".into())),
                ("sensitivity".into(), Value::String("sensitive".into())),
                ("text_sha256".into(), Value::String(DIGEST.into())),
            ])),
        );
        data.insert(
            "summary".into(),
            Value::String("Allow Agent Zero to type into the highlighted field?".into()),
        );
        request
    }

    fn ack_packet(correlation: &str, highest: u64, bridge: &str, generation: &str) -> String {
        format!(
            "42/ws,[\"connector_browser_event_ack\",{{\"handlerId\":\"{HANDLER_ID}\",\"correlationId\":\"{correlation}\",\"data\":{{\"contract_version\":1,\"bridge_id\":\"{bridge}\",\"load_generation_id\":\"{generation}\",\"highest_contiguous_event_sequence\":{highest}}}}}]"
        )
    }

    fn ack_response(request: &[u8], status: &str, highest: u64) -> RpcResponse {
        let RpcMessage::Request(request) = rpc::parse_message(request, Peer::Server).unwrap()
        else {
            panic!()
        };
        RpcResponse {
            id: request.id.unwrap(),
            result: Ok(Value::Object(BTreeMap::from([
                ("contract_version".into(), Value::Number("1".into())),
                (
                    "load_generation_id".into(),
                    Value::String(GENERATION.into()),
                ),
                (
                    "highest_contiguous_event_sequence".into(),
                    Value::Number(highest.to_string()),
                ),
                ("status".into(), Value::String(status.into())),
            ]))),
        }
    }

    #[test]
    fn event_is_retained_only_after_commit_and_exact_duplicate_does_not_requeue() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let request = lease_event("event-1", 1);
        let prepared = codec.prepare_event(&request).unwrap();
        assert!(prepared
            .packet()
            .unwrap()
            .starts_with("42/ws,[\"connector_browser_event\""));
        assert_eq!(codec.retained_count(), 0);
        codec.commit_event(prepared).unwrap();
        assert_eq!(codec.retained_count(), 1);

        let duplicate = codec.prepare_event(&request).unwrap();
        assert!(duplicate.packet().is_none());
        codec.commit_event(duplicate).unwrap();
        assert_eq!(codec.retained_count(), 1);
    }

    #[test]
    fn event_requires_exact_generation_sequence_schema_and_extension_enums() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let first = lease_event("event-1", 1);
        let first = codec.prepare_event(&first).unwrap();
        codec.commit_event(first).unwrap();
        assert!(matches!(
            codec.prepare_event(&lease_event("event-gap", 3)),
            Err(EventCodecError::InvalidPacket)
        ));

        let mut malformed = finalized_event("event-2", 2);
        let Value::Object(fields) = &mut malformed.params else {
            panic!()
        };
        fields.insert("extra".into(), Value::Bool(true));
        assert!(matches!(
            codec.prepare_event(&malformed),
            Err(EventCodecError::InvalidPacket)
        ));

        let mut invalid_count = finalized_event("event-2", 2);
        let Value::Object(fields) = &mut invalid_count.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert("error_count".into(), Value::Number("257".into()));
        assert!(matches!(
            codec.prepare_event(&invalid_count),
            Err(EventCodecError::InvalidPacket)
        ));

        let mut invalid_reason = lease_event("event-2", 2);
        let Value::Object(fields) = &mut invalid_reason.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "reason_code".into(),
            Value::String("FINALIZATION_STARTED".into()),
        );
        assert!(matches!(
            codec.prepare_event(&invalid_reason),
            Err(EventCodecError::InvalidPacket)
        ));

        let finalized = codec.prepare_event(&finalized_event("event-2", 2)).unwrap();
        codec.commit_event(finalized).unwrap();
    }

    #[test]
    fn site_challenge_requires_exact_redacted_navigation_binding() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let challenge = challenge_event("event-1", 1);
        let prepared = codec.prepare_event(&challenge).unwrap();
        assert!(prepared.packet().unwrap().contains("challenge.required"));

        let mut null_operation = challenge.clone();
        let Value::Object(fields) = &mut null_operation.params else {
            panic!()
        };
        fields.insert("op_id".into(), Value::Null);
        assert_eq!(
            codec.prepare_event(&null_operation).err(),
            Some(EventCodecError::InvalidPacket)
        );

        let mut invalid = challenge.clone();
        let Value::Object(fields) = &mut invalid.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "origin".into(),
            Value::String("https://user@example.test".into()),
        );
        assert_eq!(
            codec.prepare_event(&invalid).err(),
            Some(EventCodecError::InvalidPacket)
        );

        let mut invalid = challenge.clone();
        let Value::Object(fields) = &mut invalid.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "summary".into(),
            Value::String("unexpected\npage text".into()),
        );
        assert_eq!(
            codec.prepare_event(&invalid).err(),
            Some(EventCodecError::InvalidPacket)
        );

        let mut invalid = challenge.clone();
        let Value::Object(fields) = &mut invalid.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "target_fingerprint".into(),
            Value::String(DIGEST.to_uppercase()),
        );
        assert_eq!(
            codec.prepare_event(&invalid).err(),
            Some(EventCodecError::InvalidPacket)
        );

        let mut invalid = challenge;
        let Value::Object(fields) = &mut invalid.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert(
            "options".into(),
            Value::Array(vec![
                Value::String("allow_once".into()),
                Value::String("deny".into()),
                Value::String("allow_turn".into()),
            ]),
        );
        assert_eq!(
            codec.prepare_event(&invalid).err(),
            Some(EventCodecError::InvalidPacket)
        );
    }

    #[test]
    fn action_challenge_requires_exact_consequential_document_binding() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let challenge = action_challenge_event("event-1", 1);
        assert!(codec
            .prepare_event(&challenge)
            .unwrap()
            .packet()
            .unwrap()
            .contains("challenge.required"));

        for (key, value) in [
            ("document_id", Value::Null),
            ("action_class", Value::String("reversible_input".into())),
            ("data_classification", Value::String("text".into())),
            ("summary", Value::String(String::new())),
            (
                "options",
                Value::Array(vec![
                    Value::String("approve_once".into()),
                    Value::String("decline".into()),
                ]),
            ),
        ] {
            let mut invalid = challenge.clone();
            let Value::Object(fields) = &mut invalid.params else {
                panic!()
            };
            let Value::Object(data) = fields.get_mut("data").unwrap() else {
                panic!()
            };
            data.insert(key.into(), value);
            assert_eq!(
                codec.prepare_event(&invalid).err(),
                Some(EventCodecError::InvalidPacket),
                "invalid action challenge field {key} was accepted"
            );
        }

        let mut site_with_action_field = challenge_event("event-2", 1);
        let Value::Object(fields) = &mut site_with_action_field.params else {
            panic!()
        };
        let Value::Object(data) = fields.get_mut("data").unwrap() else {
            panic!()
        };
        data.insert("data_classification".into(), Value::String("none".into()));
        assert_eq!(
            codec.prepare_event(&site_with_action_field).err(),
            Some(EventCodecError::InvalidPacket)
        );
    }

    #[test]
    fn type_challenge_requires_exact_redacted_tagged_classification() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let challenge = type_challenge_event("event-type-1", 1);
        let packet = codec.prepare_event(&challenge).unwrap();
        let packet = packet.packet().unwrap();
        assert!(packet.contains("\"kind\":\"text\""));
        assert!(packet.contains(DIGEST));
        assert!(!packet.contains("private proposed text"));
        assert!(!packet.contains("frame0:node24"));

        for (key, value) in [
            ("action_class", Value::String("unknown".into())),
            (
                "summary",
                Value::String("Type private proposed text?".into()),
            ),
        ] {
            let mut invalid = challenge.clone();
            let Value::Object(fields) = &mut invalid.params else {
                panic!()
            };
            let Value::Object(data) = fields.get_mut("data").unwrap() else {
                panic!()
            };
            data.insert(key.into(), value);
            assert_eq!(
                codec.prepare_event(&invalid).err(),
                Some(EventCodecError::InvalidPacket)
            );
        }

        for (key, value) in [
            ("kind", Value::String("password".into())),
            ("sensitivity", Value::String("public".into())),
            ("text_sha256", Value::String(DIGEST.to_uppercase())),
            ("source", Value::String("model".into())),
        ] {
            let mut invalid = challenge.clone();
            let Value::Object(fields) = &mut invalid.params else {
                panic!()
            };
            let Value::Object(data) = fields.get_mut("data").unwrap() else {
                panic!()
            };
            let Value::Object(classification) = data.get_mut("data_classification").unwrap() else {
                panic!()
            };
            classification.insert(key.into(), value);
            assert_eq!(
                codec.prepare_event(&invalid).err(),
                Some(EventCodecError::InvalidPacket),
                "invalid type classification field {key} was accepted"
            );
        }
    }

    #[test]
    fn ack_is_credential_and_generation_bound_and_strips_bridge_from_native_params() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let event = codec.prepare_event(&lease_event("event-1", 1)).unwrap();
        codec.commit_event(event).unwrap();
        assert_eq!(
            codec.ack_command(&ack_packet("ack_1", 1, "other", GENERATION), BRIDGE, 10),
            Err(EventCodecError::InvalidPacket)
        );
        assert_eq!(
            codec.ack_command(&ack_packet("ack_1", 1, BRIDGE, "other"), BRIDGE, 10),
            Err(EventCodecError::InvalidPacket)
        );

        let request = codec
            .ack_command(&ack_packet("ack_1", 1, BRIDGE, GENERATION), BRIDGE, 10)
            .unwrap()
            .unwrap();
        let text = std::str::from_utf8(&request).unwrap();
        assert!(text.contains("\"method\":\"browser.ack_events\""));
        assert!(text.contains(&format!("\"load_generation_id\":\"{GENERATION}\"")));
        assert!(!text.contains("bridge_id"));
        assert_eq!(codec.pending_ack_count(), 1);
    }

    #[test]
    fn event_ack_requires_the_codec_transport_profile_handler() {
        let production = BrowserTransportProfile::fixture_production();
        let development = BrowserTransportProfile::fixture_development();

        let mut codec = EventCodec::with_profile(GENERATION.into(), 0, development);
        let event = codec
            .prepare_event(&lease_event("event-profile-1", 1))
            .unwrap();
        codec.commit_event(event).unwrap();
        assert_eq!(
            codec.ack_command(
                &ack_packet("ack_profile_1", 1, BRIDGE, GENERATION),
                BRIDGE,
                10,
            ),
            Err(EventCodecError::InvalidPacket)
        );

        let development_ack = ack_packet("ack_profile_1", 1, BRIDGE, GENERATION)
            .replace(production.handler_id(), development.handler_id());
        assert!(codec
            .ack_command(&development_ack, BRIDGE, 11)
            .unwrap()
            .is_some());
    }

    #[test]
    fn development_profile_accepts_site_but_not_action_challenges() {
        let development = BrowserTransportProfile::fixture_development();
        let mut codec = EventCodec::with_profile(GENERATION.into(), 0, development);
        assert!(codec
            .prepare_event(&challenge_event("event-dev-site", 1))
            .is_ok());
        assert_eq!(
            codec
                .prepare_event(&action_challenge_event("event-dev-action", 1))
                .err(),
            Some(EventCodecError::UnsupportedEvent)
        );
        assert_eq!(
            codec
                .prepare_event(&type_challenge_event("event-dev-type", 1))
                .err(),
            Some(EventCodecError::UnsupportedEvent)
        );
    }

    #[test]
    fn retained_events_release_only_after_exact_extension_ack_echo() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        for (event_id, sequence) in [("event-1", 1), ("event-2", 2)] {
            let prepared = codec
                .prepare_event(&lease_event(event_id, sequence))
                .unwrap();
            codec.commit_event(prepared).unwrap();
        }
        let request = codec
            .ack_command(&ack_packet("ack_1", 2, BRIDGE, GENERATION), BRIDGE, 10)
            .unwrap()
            .unwrap();
        let invalid = ack_response(&request, "accepted", 2);
        assert_eq!(
            codec.ack_response(&invalid, 11),
            Err(EventCodecError::InvalidResponse)
        );
        assert_eq!(codec.retained_count(), 2);

        let valid = ack_response(&request, "acknowledged", 2);
        assert_eq!(
            codec.ack_response(&valid, 12),
            Ok(AckResponseDisposition::Handled)
        );
        assert_eq!(codec.retained_count(), 0);
        assert_eq!(codec.pending_ack_count(), 0);
        assert!(codec
            .prepare_event(&lease_event("event-2", 2))
            .unwrap()
            .packet()
            .is_none());
    }

    #[test]
    fn known_late_ack_response_is_discarded_but_unknown_allocated_id_fails_closed() {
        let mut codec = EventCodec::new(GENERATION.into(), 0);
        let prepared = codec.prepare_event(&lease_event("event-1", 1)).unwrap();
        codec.commit_event(prepared).unwrap();
        let request = codec
            .ack_command(&ack_packet("ack_1", 1, BRIDGE, GENERATION), BRIDGE, 10)
            .unwrap()
            .unwrap();
        codec.expire(ACK_TIMEOUT_MS + 10);
        let late = ack_response(&request, "acknowledged", 1);
        assert_eq!(
            codec.ack_response(&late, ACK_TIMEOUT_MS + 11),
            Ok(AckResponseDisposition::Handled)
        );
        let unknown = RpcResponse {
            id: "core-event-ack-999".into(),
            result: Ok(Value::Object(BTreeMap::new())),
        };
        assert_eq!(
            codec.ack_response(&unknown, ACK_TIMEOUT_MS + 12),
            Err(EventCodecError::UnknownResponse)
        );
    }

    #[test]
    fn resumed_cursor_allows_only_the_next_generation_sequence() {
        let mut codec = EventCodec::new(GENERATION.into(), 41);
        assert!(matches!(
            codec.prepare_event(&lease_event("event-43", 43)),
            Err(EventCodecError::InvalidPacket)
        ));
        let prepared = codec.prepare_event(&lease_event("event-42", 42)).unwrap();
        codec.commit_event(prepared).unwrap();
        assert!(codec
            .ack_command(&ack_packet("ack_42", 42, BRIDGE, GENERATION), BRIDGE, 10)
            .unwrap()
            .is_some());
    }

    #[test]
    fn retained_event_and_live_ack_correlation_counts_are_bounded() {
        let mut retained = EventCodec::new(GENERATION.into(), 0);
        for sequence in 1..=MAX_RETAINED_EVENTS as u64 {
            let prepared = retained
                .prepare_event(&lease_event(&format!("event-{sequence}"), sequence))
                .unwrap();
            retained.commit_event(prepared).unwrap();
        }
        assert!(matches!(
            retained.prepare_event(&lease_event(
                "event-over-capacity",
                MAX_RETAINED_EVENTS as u64 + 1,
            )),
            Err(EventCodecError::Capacity)
        ));

        let mut correlations = EventCodec::new(GENERATION.into(), 0);
        let prepared = correlations
            .prepare_event(&lease_event("event-1", 1))
            .unwrap();
        correlations.commit_event(prepared).unwrap();
        for index in 0..MAX_PENDING_ACKS {
            assert!(correlations
                .ack_command(
                    &ack_packet(&format!("ack_{index}"), 1, BRIDGE, GENERATION),
                    BRIDGE,
                    10,
                )
                .unwrap()
                .is_some());
        }
        assert_eq!(correlations.pending_ack_count(), MAX_PENDING_ACKS);
        assert_eq!(
            correlations.ack_command(
                &ack_packet("ack_over_capacity", 1, BRIDGE, GENERATION),
                BRIDGE,
                10,
            ),
            Err(EventCodecError::Capacity)
        );
    }
}
