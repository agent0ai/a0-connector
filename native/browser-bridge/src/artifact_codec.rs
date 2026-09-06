//! Bounded output-artifact mapping between extension JSON-RPC and Core events.
//!
//! This codec never derives authority from an artifact frame. Every frame must
//! match a still-pending browser operation retained by `ConnectorCodec`; Core
//! independently resolves and spools the same binding against its authenticated
//! principal. After full runtime admission, this codec also verifies every
//! output phase in the private native spool before forwarding it to Core and
//! retains the bytes until the exact operation result consumes them.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::Path;

use base64::{engine::general_purpose::STANDARD, Engine};
use sha2::{Digest, Sha256};

use crate::artifact::{
    ArtifactBinding, ArtifactDirection, ArtifactError, ArtifactLimits, ArtifactPurpose,
    ArtifactRoute, ArtifactSpool, CurrentRouteAuthorizer,
};
use crate::connector_codec::{
    ConnectorCodec, OperationSettlement, PendingOperationBinding, VerifiedArtifactClaim,
};
use crate::json::{self, Value};
use crate::rpc::{self, Peer, RpcErrorObject, RpcMessage, RpcRequest, RpcResponse};
use crate::transport_profile::BrowserTransportProfile;
#[cfg(test)]
use crate::transport_profile::PRODUCTION_HANDLER_ID as HANDLER_ID;

const MAX_ACTIVE_ARTIFACTS: usize = 16;
const MAX_PENDING_FRAMES: usize = 64;
const MAX_COMPLETED: usize = 2_048;
const MAX_TOTAL_DECLARED_BYTES: u64 = 100 * 1024 * 1024;
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;
const FRAME_TIMEOUT_MS: u64 = rpc::MAX_REQUEST_TIMEOUT_MS;

const CORE_COMMON_KEYS: &[&str] = &[
    "contract_version",
    "phase",
    "bridge_id",
    "load_generation_id",
    "context_id",
    "browser_session_id",
    "turn_id",
    "action_id",
    "op_id",
    "artifact_id",
    "direction",
    "purpose",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ArtifactCodecError {
    InvalidPacket,
    UnsupportedEvent,
    InvalidResponse,
    UnknownResponse,
    Capacity,
    ClockInvalid,
    PrivateSpoolUnavailable,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Binding {
    bridge_id: String,
    load_generation_id: String,
    context_id: String,
    browser_session_id: String,
    turn_id: String,
    action_id: String,
    op_id: String,
    artifact_id: String,
    purpose: String,
}

struct Transfer {
    binding: Binding,
    mime_type: String,
    byte_count: u64,
    sha256: String,
    expected_digest: [u8; 32],
    digest: Sha256,
    received_bytes: u64,
    next_chunk_index: u64,
}

enum PendingEffect {
    Begin {
        mime_type: String,
        byte_count: u64,
        sha256: String,
        expected_digest: [u8; 32],
    },
    Chunk {
        digest: Sha256,
        received_bytes: u64,
        next_chunk_index: u64,
        data: Vec<u8>,
    },
    End,
    Abort,
}

struct PendingFrame {
    request_id: String,
    binding: Binding,
    phase: &'static str,
    effect: PendingEffect,
    deadline_ms: u64,
}

struct PrivateSpool {
    route: ArtifactRoute,
    spool: ArtifactSpool,
    bindings: BTreeMap<String, ArtifactBinding>,
    descriptor_ready: BTreeSet<String>,
    input_paths_issued: BTreeSet<String>,
}

pub(crate) struct ArtifactCodec {
    transport_profile: BrowserTransportProfile,
    last_now_ms: u64,
    pending: BTreeMap<String, PendingFrame>,
    transfers: BTreeMap<String, Transfer>,
    reserved_bytes: u64,
    completed_request_ids: BTreeSet<String>,
    completed_request_order: VecDeque<String>,
    completed_frames: BTreeSet<String>,
    completed_frame_order: VecDeque<String>,
    terminal_artifacts: BTreeSet<String>,
    terminal_artifact_order: VecDeque<String>,
    private_spool: Option<PrivateSpool>,
}

impl ArtifactCodec {
    pub(crate) fn new(started_at_ms: u64) -> Self {
        Self::with_profile(started_at_ms, BrowserTransportProfile::compiled())
    }

    pub(crate) fn with_profile(
        started_at_ms: u64,
        transport_profile: BrowserTransportProfile,
    ) -> Self {
        Self {
            transport_profile,
            last_now_ms: started_at_ms,
            pending: BTreeMap::new(),
            transfers: BTreeMap::new(),
            reserved_bytes: 0,
            completed_request_ids: BTreeSet::new(),
            completed_request_order: VecDeque::new(),
            completed_frames: BTreeSet::new(),
            completed_frame_order: VecDeque::new(),
            terminal_artifacts: BTreeSet::new(),
            terminal_artifact_order: VecDeque::new(),
            private_spool: None,
        }
    }

    pub(crate) fn attach_private_spool(
        &mut self,
        route: ArtifactRoute,
        authorizer: CurrentRouteAuthorizer,
        started_at_ms: u64,
        spool_parent: Option<&Path>,
    ) -> Result<(), ArtifactCodecError> {
        if self.private_spool.is_some()
            || !self.pending.is_empty()
            || !self.transfers.is_empty()
            || !self.terminal_artifacts.is_empty()
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let spool = ArtifactSpool::new(
            spool_parent,
            authorizer,
            started_at_ms,
            ArtifactLimits::default(),
        )
        .map_err(map_spool_error)?;
        self.private_spool = Some(PrivateSpool {
            route,
            spool,
            bindings: BTreeMap::new(),
            descriptor_ready: BTreeSet::new(),
            input_paths_issued: BTreeSet::new(),
        });
        Ok(())
    }

    pub(crate) fn request(
        &mut self,
        request: &RpcRequest,
        operations: &ConnectorCodec,
        now_ms: u64,
    ) -> Result<Vec<u8>, ArtifactCodecError> {
        self.observe(now_ms)?;
        if operations.transport_profile() != self.transport_profile {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let request_id = request
            .id
            .as_deref()
            .filter(|value| rpc::valid_opaque_id(value))
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        if self.completed_request_ids.contains(request_id)
            || self
                .pending
                .values()
                .any(|pending| pending.request_id == request_id)
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let encoded = RpcMessage::Request(request.clone()).encode();
        rpc::parse_message(&encoded, Peer::Extension)
            .map_err(|_| ArtifactCodecError::InvalidPacket)?;
        let fields = request
            .params
            .as_object()
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let operation = operations
            .pending_operation(identifier(fields, "op_id")?)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let binding = binding(fields, &operation)?;
        if self.pending.contains_key(&binding.artifact_id)
            || self.terminal_artifacts.contains(&binding.artifact_id)
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        if self.pending.len() >= MAX_PENDING_FRAMES {
            return Err(ArtifactCodecError::Capacity);
        }
        let phase = artifact_phase(&request.method)?;
        let effect = match phase {
            "begin" => self.prepare_begin(fields, &binding)?,
            "chunk" => self.prepare_chunk(fields, &binding)?,
            "end" => self.prepare_end(&binding)?,
            "abort" => self.prepare_abort(&binding)?,
            _ => return Err(ArtifactCodecError::UnsupportedEvent),
        };
        let deadline_ms = now_ms
            .checked_add(FRAME_TIMEOUT_MS)
            .ok_or(ArtifactCodecError::ClockInvalid)?;
        let packet = core_packet(fields, &binding, phase)?;
        self.apply_private_phase(&binding, &effect, now_ms)?;
        self.pending.insert(
            binding.artifact_id.clone(),
            PendingFrame {
                request_id: request_id.to_owned(),
                binding,
                phase,
                effect,
                deadline_ms,
            },
        );
        Ok(packet.into_bytes())
    }

    /// Receive Core-owned upload bytes into the private verified native spool.
    /// An ACK proves transport integrity only, never file-input consent or use.
    pub(crate) fn input_packet(
        &mut self,
        packet: &str,
        credential_bridge_id: &str,
        operations: &ConnectorCodec,
        now_ms: u64,
    ) -> Result<Vec<u8>, ArtifactCodecError> {
        self.observe(now_ms)?;
        if packet.len() > rpc::MAX_NATIVE_FRAME_BYTES {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let root = json::parse(
            packet
                .strip_prefix("42/ws,")
                .ok_or(ArtifactCodecError::InvalidPacket)?
                .as_bytes(),
        )
        .map_err(|_| ArtifactCodecError::InvalidPacket)?;
        let root = root
            .as_array()
            .filter(|value| value.len() == 2)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        if root[0].as_str() != Some("connector_browser_artifact_chunk") {
            return Err(ArtifactCodecError::UnsupportedEvent);
        }
        if !self.transport_profile.permits_context()
            || operations.transport_profile() != self.transport_profile
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let envelope = root[1]
            .as_object()
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        validate_envelope(envelope, self.transport_profile.handler_id())?;
        let fields = envelope
            .get("data")
            .and_then(Value::as_object)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let phase = fields
            .get("phase")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let additional: &[&str] = match phase {
            "begin" => &["mime_type", "byte_count", "sha256"],
            "chunk" => &["chunk_index", "data_base64"],
            "end" => &[],
            "abort" => &["reason_code"],
            _ => return Err(ArtifactCodecError::InvalidPacket),
        };
        if fields.len() != CORE_COMMON_KEYS.len() + additional.len()
            || CORE_COMMON_KEYS
                .iter()
                .chain(additional)
                .any(|key| !fields.contains_key(*key))
            || integer(fields, "contract_version")? != 1
            || identifier(fields, "bridge_id")? != credential_bridge_id
            || fields.get("direction").and_then(Value::as_str) != Some("input")
            || fields.get("purpose").and_then(Value::as_str) != Some("upload_file")
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let artifact_id = identifier(fields, "artifact_id")?;
        if envelope.get("correlationId").and_then(Value::as_str) != Some(artifact_id) {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let operation = operations
            .pending_operation(identifier(fields, "op_id")?)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        if operation.action != "upload_file"
            || operation.bridge_id != credential_bridge_id
            || operation.load_generation_id != identifier(fields, "load_generation_id")?
            || operation.context_id != identifier(fields, "context_id")?
            || operation.browser_session_id != identifier(fields, "browser_session_id")?
            || operation.turn_id != identifier(fields, "turn_id")?
            || operation.action_id != identifier(fields, "action_id")?
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let private = self
            .private_spool
            .as_mut()
            .ok_or(ArtifactCodecError::PrivateSpoolUnavailable)?;
        if private.route.bridge_id() != credential_bridge_id
            || private.route.load_generation_id() != operation.load_generation_id
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let binding = ArtifactBinding::new(
            &private.route,
            &operation.context_id,
            &operation.browser_session_id,
            &operation.turn_id,
            &operation.action_id,
            &operation.op_id,
            artifact_id,
            ArtifactDirection::Input,
            ArtifactPurpose::UploadFile,
        )
        .map_err(map_spool_error)?;
        let mut ack: BTreeMap<String, Value> = CORE_COMMON_KEYS
            .iter()
            .map(|key| ((*key).into(), fields[*key].clone()))
            .collect();
        match phase {
            "begin" | "chunk" => {
                let progress = if phase == "begin" {
                    if private.bindings.contains_key(artifact_id) {
                        return Err(ArtifactCodecError::InvalidPacket);
                    }
                    let count = integer(fields, "byte_count")?;
                    let digest = fields
                        .get("sha256")
                        .and_then(Value::as_str)
                        .ok_or(ArtifactCodecError::InvalidPacket)?;
                    let mime = fields
                        .get("mime_type")
                        .and_then(Value::as_str)
                        .ok_or(ArtifactCodecError::InvalidPacket)?;
                    let progress = private
                        .spool
                        .begin(&binding, count, digest, mime, now_ms)
                        .map_err(map_spool_error)?;
                    private.bindings.insert(artifact_id.into(), binding.clone());
                    progress
                } else {
                    if private.bindings.get(artifact_id) != Some(&binding) {
                        return Err(ArtifactCodecError::InvalidPacket);
                    }
                    let encoded = fields
                        .get("data_base64")
                        .and_then(Value::as_str)
                        .ok_or(ArtifactCodecError::InvalidPacket)?;
                    if encoded.len() > 256 * 1024 {
                        return Err(ArtifactCodecError::InvalidPacket);
                    }
                    let data = STANDARD
                        .decode(encoded)
                        .map_err(|_| ArtifactCodecError::InvalidPacket)?;
                    if data.is_empty()
                        || data.len() > rpc::MAX_ARTIFACT_CHUNK_RAW_BYTES
                        || STANDARD.encode(&data) != encoded
                    {
                        return Err(ArtifactCodecError::InvalidPacket);
                    }
                    private
                        .spool
                        .append(&binding, integer(fields, "chunk_index")?, &data, now_ms)
                        .map_err(map_spool_error)?
                };
                ack.insert("status".into(), Value::String(progress.status.into()));
                ack.insert(
                    "next_chunk_index".into(),
                    Value::Number(progress.next_chunk_index.to_string()),
                );
                ack.insert(
                    "received_bytes".into(),
                    Value::Number(progress.received_bytes.to_string()),
                );
            }
            "end" => {
                if private.bindings.get(artifact_id) != Some(&binding)
                    || private.descriptor_ready.contains(artifact_id)
                    || private.input_paths_issued.contains(artifact_id)
                {
                    return Err(ArtifactCodecError::InvalidPacket);
                }
                let descriptor = private
                    .spool
                    .complete(&binding, now_ms)
                    .map_err(map_spool_error)?;
                private.descriptor_ready.insert(artifact_id.into());
                ack.insert("status".into(), Value::String("complete".into()));
                ack.insert(
                    "descriptor".into(),
                    Value::Object(BTreeMap::from([
                        (
                            "artifact_id".into(),
                            Value::String(descriptor.artifact_id().into()),
                        ),
                        (
                            "mime_type".into(),
                            Value::String(descriptor.mime_type().into()),
                        ),
                        (
                            "byte_count".into(),
                            Value::Number(descriptor.byte_count().to_string()),
                        ),
                        ("sha256".into(), Value::String(descriptor.sha256().into())),
                        ("purpose".into(), Value::String("upload_file".into())),
                    ])),
                );
            }
            "abort" => {
                let reason = fields
                    .get("reason_code")
                    .and_then(Value::as_str)
                    .filter(|value| valid_abort_reason(value))
                    .ok_or(ArtifactCodecError::InvalidPacket)?;
                private
                    .spool
                    .abort(&binding, now_ms)
                    .map_err(map_spool_error)?;
                private.bindings.remove(artifact_id);
                private.descriptor_ready.remove(artifact_id);
                private.input_paths_issued.remove(artifact_id);
                ack.insert("status".into(), Value::String("aborted".into()));
                ack.insert("reason_code".into(), Value::String(reason.into()));
            }
            _ => unreachable!(),
        }
        Ok(format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_artifact_ack".into()),
                Value::Object(BTreeMap::from([
                    ("correlationId".into(), Value::String(artifact_id.into())),
                    ("data".into(), Value::Object(ack)),
                ])),
            ])
            .encode()
        )
        .into_bytes())
    }

    pub(crate) fn input_ready(&self, op_id: &str, artifact_id: &str) -> bool {
        self.private_spool.as_ref().is_some_and(|private| {
            private.descriptor_ready.contains(artifact_id)
                && private.bindings.get(artifact_id).is_some_and(|binding| {
                    binding.op_id() == op_id
                        && binding.direction() == ArtifactDirection::Input
                        && binding.purpose() == ArtifactPurpose::UploadFile
                })
        })
    }

    pub(crate) fn input_path_response(
        &mut self,
        request: &RpcRequest,
        operations: &ConnectorCodec,
        now_ms: u64,
    ) -> Result<Vec<u8>, ArtifactCodecError> {
        self.observe(now_ms)?;
        if !self.transport_profile.permits_context() || request.method != "artifact.input_path" {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        rpc::parse_message(
            &RpcMessage::Request(request.clone()).encode(),
            Peer::Extension,
        )
        .map_err(|_| ArtifactCodecError::InvalidPacket)?;
        let fields = request
            .params
            .as_object()
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let operation = operations
            .pending_operation(identifier(fields, "op_id")?)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let artifact_id = identifier(fields, "artifact_id")?;
        let private = self
            .private_spool
            .as_mut()
            .ok_or(ArtifactCodecError::PrivateSpoolUnavailable)?;
        let binding = private
            .bindings
            .get(artifact_id)
            .cloned()
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        if operations.transport_profile() != self.transport_profile
            || operation.action != "upload_file"
            || !exact_operation_binding(&binding, &operation)
            || binding.direction() != ArtifactDirection::Input
            || binding.purpose() != ArtifactPurpose::UploadFile
            || binding.context_id() != identifier(fields, "context_id")?
            || binding.browser_session_id() != identifier(fields, "browser_session_id")?
            || binding.turn_id() != identifier(fields, "turn_id")?
            || binding.action_id() != identifier(fields, "action_id")?
            || !private.descriptor_ready.contains(artifact_id)
            || private.input_paths_issued.contains(artifact_id)
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let (path, descriptor) = private
            .spool
            .input_path(&binding, now_ms)
            .map_err(map_spool_error)?;
        let path = path
            .to_str()
            .filter(|value| value.len() <= 4096)
            .ok_or(ArtifactCodecError::PrivateSpoolUnavailable)?;
        let mut result = fields.clone();
        result.insert("ephemeral_path".into(), Value::String(path.into()));
        result.insert(
            "descriptor".into(),
            Value::Object(BTreeMap::from([
                (
                    "artifact_id".into(),
                    Value::String(descriptor.artifact_id().into()),
                ),
                (
                    "mime_type".into(),
                    Value::String(descriptor.mime_type().into()),
                ),
                (
                    "byte_count".into(),
                    Value::Number(descriptor.byte_count().to_string()),
                ),
                ("sha256".into(), Value::String(descriptor.sha256().into())),
                ("purpose".into(), Value::String("upload_file".into())),
            ])),
        );
        private.input_paths_issued.insert(artifact_id.into());
        Ok(RpcMessage::Response(RpcResponse {
            id: request
                .id
                .clone()
                .ok_or(ArtifactCodecError::InvalidPacket)?,
            result: Ok(Value::Object(result)),
        })
        .encode())
    }

    pub(crate) fn acknowledgement(
        &mut self,
        packet: &str,
        credential_bridge_id: &str,
        now_ms: u64,
    ) -> Result<Option<Vec<u8>>, ArtifactCodecError> {
        self.observe(now_ms)?;
        if packet.len() > rpc::MAX_NATIVE_FRAME_BYTES {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let array = json::parse(
            packet
                .strip_prefix("42/ws,")
                .ok_or(ArtifactCodecError::InvalidPacket)?
                .as_bytes(),
        )
        .map_err(|_| ArtifactCodecError::InvalidPacket)?;
        let array = array
            .as_array()
            .filter(|values| values.len() == 2)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        if array[0].as_str() != Some("connector_browser_artifact_ack") {
            return Err(ArtifactCodecError::UnsupportedEvent);
        }
        let envelope = array[1]
            .as_object()
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        validate_envelope(envelope, self.transport_profile.handler_id())?;
        let data = envelope
            .get("data")
            .and_then(Value::as_object)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let artifact_id = identifier(data, "artifact_id")?.to_owned();
        let binding = binding_from_ack(data, credential_bridge_id)?;
        if envelope.get("correlationId").and_then(Value::as_str) != Some(&binding.op_id) {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let phase = data
            .get("phase")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        validate_ack_shape(data, phase)?;

        let Some(pending) = self.pending.remove(&artifact_id) else {
            return if self.completed_frames.contains(&frame_key(&binding, phase)) {
                Ok(None)
            } else {
                Err(ArtifactCodecError::UnknownResponse)
            };
        };
        if pending.binding != binding || pending.phase != phase {
            self.abort_private(&pending.binding, now_ms);
            self.finish_terminal(&pending.binding);
            self.remember_request(pending.request_id);
            self.remember_frame(frame_key(&pending.binding, pending.phase));
            return Err(ArtifactCodecError::InvalidResponse);
        }
        if now_ms >= pending.deadline_ms {
            self.abort_private(&pending.binding, now_ms);
            self.finish_terminal(&pending.binding);
            self.remember_request(pending.request_id);
            self.remember_frame(frame_key(&pending.binding, pending.phase));
            return Ok(None);
        }
        if let Err(error) = self.validate_and_apply_ack(data, &pending, now_ms) {
            self.abort_private(&pending.binding, now_ms);
            self.finish_terminal(&pending.binding);
            self.remember_request(pending.request_id);
            self.remember_frame(frame_key(&pending.binding, pending.phase));
            return Err(error);
        }
        self.remember_request(pending.request_id.clone());
        self.remember_frame(frame_key(&pending.binding, pending.phase));

        let mut native_result = data.clone();
        native_result.remove("bridge_id");
        native_result.remove("load_generation_id");
        Ok(Some(
            RpcMessage::Response(RpcResponse {
                id: pending.request_id,
                result: Ok(Value::Object(native_result)),
            })
            .encode(),
        ))
    }

    pub(crate) fn expire(&mut self, now_ms: u64) -> Result<Vec<Vec<u8>>, ArtifactCodecError> {
        self.observe(now_ms)?;
        if let Some(private) = self.private_spool.as_mut() {
            private.spool.expire(now_ms).map_err(map_spool_error)?;
            private
                .bindings
                .retain(|_, binding| private.spool.contains_exact(binding));
            private
                .descriptor_ready
                .retain(|artifact_id| private.bindings.contains_key(artifact_id));
            private
                .input_paths_issued
                .retain(|artifact_id| private.bindings.contains_key(artifact_id));
        }
        let expired = self
            .pending
            .iter()
            .filter(|(_, pending)| now_ms >= pending.deadline_ms)
            .map(|(artifact_id, _)| artifact_id.clone())
            .collect::<Vec<_>>();
        let mut responses = Vec::with_capacity(expired.len());
        for artifact_id in expired {
            let pending = self
                .pending
                .remove(&artifact_id)
                .expect("expired pending artifact exists");
            self.abort_private(&pending.binding, now_ms);
            self.finish_terminal(&pending.binding);
            self.remember_request(pending.request_id.clone());
            self.remember_frame(frame_key(&pending.binding, pending.phase));
            responses.push(timeout_response(&pending.request_id));
        }
        Ok(responses)
    }

    fn prepare_begin(
        &self,
        fields: &BTreeMap<String, Value>,
        binding: &Binding,
    ) -> Result<PendingEffect, ArtifactCodecError> {
        let pending_begins = self
            .pending
            .values()
            .filter_map(|pending| match &pending.effect {
                PendingEffect::Begin { byte_count, .. } => Some(*byte_count),
                _ => None,
            })
            .collect::<Vec<_>>();
        if self.transfers.contains_key(&binding.artifact_id) {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        if self
            .transfers
            .len()
            .checked_add(pending_begins.len())
            .is_none_or(|count| count >= MAX_ACTIVE_ARTIFACTS)
        {
            return Err(ArtifactCodecError::Capacity);
        }
        let byte_count = integer(fields, "byte_count")?;
        let pending_bytes = pending_begins.into_iter().try_fold(0_u64, u64::checked_add);
        if pending_bytes
            .and_then(|pending| self.reserved_bytes.checked_add(pending))
            .and_then(|reserved| reserved.checked_add(byte_count))
            .is_none_or(|value| value > MAX_TOTAL_DECLARED_BYTES)
        {
            return Err(ArtifactCodecError::Capacity);
        }
        let sha256 = fields
            .get("sha256")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidPacket)?
            .to_owned();
        let mime_type = fields
            .get("mime_type")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidPacket)?
            .to_owned();
        Ok(PendingEffect::Begin {
            mime_type,
            byte_count,
            expected_digest: parse_sha256(&sha256)?,
            sha256,
        })
    }

    fn prepare_chunk(
        &self,
        fields: &BTreeMap<String, Value>,
        binding: &Binding,
    ) -> Result<PendingEffect, ArtifactCodecError> {
        let transfer = self.exact_transfer(binding)?;
        let chunk_index = integer(fields, "chunk_index")?;
        if chunk_index != transfer.next_chunk_index {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let encoded = fields
            .get("data")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let bytes = STANDARD
            .decode(encoded)
            .map_err(|_| ArtifactCodecError::InvalidPacket)?;
        if bytes.is_empty()
            || bytes.len() > rpc::MAX_ARTIFACT_CHUNK_RAW_BYTES
            || STANDARD.encode(&bytes) != encoded
        {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        let received_bytes = transfer
            .received_bytes
            .checked_add(u64::try_from(bytes.len()).map_err(|_| ArtifactCodecError::Capacity)?)
            .filter(|value| *value <= transfer.byte_count)
            .ok_or(ArtifactCodecError::InvalidPacket)?;
        let next_chunk_index = transfer
            .next_chunk_index
            .checked_add(1)
            .filter(|value| *value <= MAX_SAFE_INTEGER)
            .ok_or(ArtifactCodecError::Capacity)?;
        let mut digest = transfer.digest.clone();
        digest.update(&bytes);
        Ok(PendingEffect::Chunk {
            digest,
            received_bytes,
            next_chunk_index,
            data: bytes,
        })
    }

    fn prepare_end(&self, binding: &Binding) -> Result<PendingEffect, ArtifactCodecError> {
        let transfer = self.exact_transfer(binding)?;
        let digest: [u8; 32] = transfer.digest.clone().finalize().into();
        if transfer.received_bytes != transfer.byte_count || digest != transfer.expected_digest {
            return Err(ArtifactCodecError::InvalidPacket);
        }
        Ok(PendingEffect::End)
    }

    fn prepare_abort(&self, binding: &Binding) -> Result<PendingEffect, ArtifactCodecError> {
        if let Some(transfer) = self.transfers.get(&binding.artifact_id) {
            if transfer.binding != *binding {
                return Err(ArtifactCodecError::InvalidPacket);
            }
        }
        Ok(PendingEffect::Abort)
    }

    fn exact_transfer(&self, binding: &Binding) -> Result<&Transfer, ArtifactCodecError> {
        self.transfers
            .get(&binding.artifact_id)
            .filter(|transfer| transfer.binding == *binding)
            .ok_or(ArtifactCodecError::InvalidPacket)
    }

    fn apply_private_phase(
        &mut self,
        binding: &Binding,
        effect: &PendingEffect,
        now_ms: u64,
    ) -> Result<(), ArtifactCodecError> {
        let Some(private) = self.private_spool.as_mut() else {
            return Ok(());
        };
        let purpose = match binding.purpose.as_str() {
            "screenshot" => ArtifactPurpose::Screenshot,
            "download" => ArtifactPurpose::Download,
            _ => return Err(ArtifactCodecError::InvalidPacket),
        };
        let artifact_binding = ArtifactBinding::new(
            &private.route,
            &binding.context_id,
            &binding.browser_session_id,
            &binding.turn_id,
            &binding.action_id,
            &binding.op_id,
            &binding.artifact_id,
            ArtifactDirection::Output,
            purpose,
        )
        .map_err(map_spool_error)?;
        match effect {
            PendingEffect::Begin {
                mime_type,
                byte_count,
                sha256,
                ..
            } => {
                let progress = private
                    .spool
                    .begin(&artifact_binding, *byte_count, sha256, mime_type, now_ms)
                    .map_err(map_spool_error)?;
                if progress.next_chunk_index != 0 || progress.received_bytes != 0 {
                    let _ = private.spool.abort(&artifact_binding, now_ms);
                    return Err(ArtifactCodecError::InvalidPacket);
                }
                private
                    .bindings
                    .insert(binding.artifact_id.clone(), artifact_binding);
            }
            PendingEffect::Chunk {
                received_bytes,
                next_chunk_index,
                data,
                ..
            } => {
                let current = private
                    .bindings
                    .get(&binding.artifact_id)
                    .filter(|candidate| exact_private_binding(candidate, binding))
                    .ok_or(ArtifactCodecError::InvalidPacket)?;
                let progress = private
                    .spool
                    .append(current, next_chunk_index.saturating_sub(1), data, now_ms)
                    .map_err(map_spool_error)?;
                if progress.next_chunk_index != *next_chunk_index
                    || progress.received_bytes != *received_bytes
                {
                    let _ = private.spool.abort(current, now_ms);
                    private.bindings.remove(&binding.artifact_id);
                    return Err(ArtifactCodecError::InvalidPacket);
                }
            }
            PendingEffect::End => {
                let current = private
                    .bindings
                    .get(&binding.artifact_id)
                    .filter(|candidate| exact_private_binding(candidate, binding))
                    .ok_or(ArtifactCodecError::InvalidPacket)?;
                private
                    .spool
                    .complete(current, now_ms)
                    .map_err(map_spool_error)?;
            }
            PendingEffect::Abort => {
                if let Some(current) = private.bindings.remove(&binding.artifact_id) {
                    private.descriptor_ready.remove(&binding.artifact_id);
                    private
                        .spool
                        .abort(&current, now_ms)
                        .map_err(map_spool_error)?;
                }
            }
        }
        Ok(())
    }

    fn mark_private_ready(
        &mut self,
        binding: &Binding,
        data: &BTreeMap<String, Value>,
        now_ms: u64,
    ) -> Result<(), ArtifactCodecError> {
        let Some(private) = self.private_spool.as_mut() else {
            return Ok(());
        };
        let current = private
            .bindings
            .get(&binding.artifact_id)
            .filter(|candidate| exact_private_binding(candidate, binding))
            .ok_or(ArtifactCodecError::InvalidResponse)?;
        let descriptor = private
            .spool
            .complete(current, now_ms)
            .map_err(map_spool_error)?;
        let claimed = data
            .get("descriptor")
            .and_then(Value::as_object)
            .ok_or(ArtifactCodecError::InvalidResponse)?;
        if !descriptor_matches_fields(&descriptor, claimed) {
            return Err(ArtifactCodecError::InvalidResponse);
        }
        private.descriptor_ready.insert(binding.artifact_id.clone());
        Ok(())
    }

    fn abort_private(&mut self, binding: &Binding, now_ms: u64) {
        let Some(private) = self.private_spool.as_mut() else {
            return;
        };
        private.descriptor_ready.remove(&binding.artifact_id);
        if let Some(current) = private.bindings.remove(&binding.artifact_id) {
            let _ = private.spool.abort(&current, now_ms);
        }
    }

    pub(crate) fn settle_operation(
        &mut self,
        settlement: &OperationSettlement,
        now_ms: u64,
    ) -> Result<(), ArtifactCodecError> {
        self.observe(now_ms)?;
        let Some(private) = self.private_spool.as_mut() else {
            return if !settlement.succeeded
                || (settlement.artifacts.is_empty()
                    && !matches!(
                        settlement.binding.action.as_str(),
                        "screenshot" | "download"
                    ))
            {
                Ok(())
            } else {
                Err(ArtifactCodecError::PrivateSpoolUnavailable)
            };
        };
        let artifact_ids = private
            .bindings
            .iter()
            .filter(|(_, binding)| exact_operation_binding(binding, &settlement.binding))
            .map(|(artifact_id, _)| artifact_id.clone())
            .collect::<Vec<_>>();
        if !settlement.succeeded {
            cleanup_private_ids(private, artifact_ids, now_ms);
            return Ok(());
        }
        if settlement.binding.action == "upload_file" {
            if !settlement.artifacts.is_empty() {
                cleanup_private_ids(private, artifact_ids, now_ms);
                return Err(ArtifactCodecError::InvalidResponse);
            }
            cleanup_private_ids(private, artifact_ids, now_ms);
            return Ok(());
        }
        if !matches!(
            settlement.binding.action.as_str(),
            "screenshot" | "download"
        ) {
            if !artifact_ids.is_empty() || !settlement.artifacts.is_empty() {
                cleanup_private_ids(private, artifact_ids, now_ms);
                return Err(ArtifactCodecError::InvalidResponse);
            }
            return Ok(());
        }
        if artifact_ids.len() != 1 || settlement.artifacts.len() != 1 {
            cleanup_private_ids(private, artifact_ids, now_ms);
            return Err(ArtifactCodecError::InvalidResponse);
        }
        let artifact_id = &artifact_ids[0];
        let claim = &settlement.artifacts[0];
        if claim.artifact_id != *artifact_id || !private.descriptor_ready.contains(artifact_id) {
            cleanup_private_ids(private, artifact_ids, now_ms);
            return Err(ArtifactCodecError::InvalidResponse);
        }
        let binding = private
            .bindings
            .get(artifact_id)
            .cloned()
            .ok_or(ArtifactCodecError::InvalidResponse)?;
        let consumed = private
            .spool
            .consume(&binding, now_ms, |_file, descriptor| {
                if descriptor_matches_claim(descriptor, claim) {
                    Ok(())
                } else {
                    Err(ArtifactError::ArtifactIntegrityMismatch)
                }
            });
        private.bindings.remove(artifact_id);
        private.descriptor_ready.remove(artifact_id);
        consumed.map_err(map_spool_error)
    }

    fn validate_and_apply_ack(
        &mut self,
        data: &BTreeMap<String, Value>,
        pending: &PendingFrame,
        now_ms: u64,
    ) -> Result<(), ArtifactCodecError> {
        let status = data
            .get("status")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidResponse)?;
        if status == "aborted" {
            self.abort_private(&pending.binding, now_ms);
            self.finish_terminal(&pending.binding);
            return Ok(());
        }
        match &pending.effect {
            PendingEffect::Begin {
                mime_type,
                byte_count,
                sha256,
                expected_digest,
            } => {
                if !matches!(status, "accepted" | "duplicate")
                    || integer_response(data, "next_chunk_index")? != 0
                    || integer_response(data, "received_bytes")? != 0
                {
                    return Err(ArtifactCodecError::InvalidResponse);
                }
                self.reserved_bytes = self
                    .reserved_bytes
                    .checked_add(*byte_count)
                    .ok_or(ArtifactCodecError::Capacity)?;
                self.transfers.insert(
                    pending.binding.artifact_id.clone(),
                    Transfer {
                        binding: pending.binding.clone(),
                        mime_type: mime_type.clone(),
                        byte_count: *byte_count,
                        sha256: sha256.clone(),
                        expected_digest: *expected_digest,
                        digest: Sha256::new(),
                        received_bytes: 0,
                        next_chunk_index: 0,
                    },
                );
            }
            PendingEffect::Chunk {
                digest,
                received_bytes,
                next_chunk_index,
                ..
            } => {
                if !matches!(status, "accepted" | "duplicate")
                    || integer_response(data, "next_chunk_index")? != *next_chunk_index
                    || integer_response(data, "received_bytes")? != *received_bytes
                {
                    return Err(ArtifactCodecError::InvalidResponse);
                }
                let transfer = self
                    .transfers
                    .get_mut(&pending.binding.artifact_id)
                    .filter(|transfer| transfer.binding == pending.binding)
                    .ok_or(ArtifactCodecError::InvalidResponse)?;
                transfer.digest = digest.clone();
                transfer.received_bytes = *received_bytes;
                transfer.next_chunk_index = *next_chunk_index;
            }
            PendingEffect::End => {
                if status != "complete" {
                    return Err(ArtifactCodecError::InvalidResponse);
                }
                let transfer = self.exact_transfer(&pending.binding)?;
                validate_descriptor(data, transfer)?;
                self.mark_private_ready(&pending.binding, data, now_ms)?;
                self.finish_terminal(&pending.binding);
            }
            PendingEffect::Abort => {
                if status != "aborted" {
                    return Err(ArtifactCodecError::InvalidResponse);
                }
                self.abort_private(&pending.binding, now_ms);
                self.finish_terminal(&pending.binding);
            }
        }
        Ok(())
    }

    fn finish_terminal(&mut self, binding: &Binding) {
        if let Some(transfer) = self.transfers.remove(&binding.artifact_id) {
            self.reserved_bytes = self.reserved_bytes.saturating_sub(transfer.byte_count);
        }
        if self.terminal_artifacts.insert(binding.artifact_id.clone()) {
            self.terminal_artifact_order
                .push_back(binding.artifact_id.clone());
        }
        while self.terminal_artifact_order.len() > MAX_COMPLETED {
            if let Some(oldest) = self.terminal_artifact_order.pop_front() {
                self.terminal_artifacts.remove(&oldest);
            }
        }
    }

    fn remember_request(&mut self, request_id: String) {
        if self.completed_request_ids.insert(request_id.clone()) {
            self.completed_request_order.push_back(request_id);
        }
        while self.completed_request_order.len() > MAX_COMPLETED {
            if let Some(oldest) = self.completed_request_order.pop_front() {
                self.completed_request_ids.remove(&oldest);
            }
        }
    }

    fn remember_frame(&mut self, key: String) {
        if self.completed_frames.insert(key.clone()) {
            self.completed_frame_order.push_back(key);
        }
        while self.completed_frame_order.len() > MAX_COMPLETED {
            if let Some(oldest) = self.completed_frame_order.pop_front() {
                self.completed_frames.remove(&oldest);
            }
        }
    }

    fn observe(&mut self, now_ms: u64) -> Result<(), ArtifactCodecError> {
        if now_ms < self.last_now_ms {
            return Err(ArtifactCodecError::ClockInvalid);
        }
        self.last_now_ms = now_ms;
        Ok(())
    }

    #[cfg(test)]
    fn active_count(&self) -> usize {
        self.transfers.len()
    }

    #[cfg(test)]
    fn private_active_count(&self) -> usize {
        self.private_spool
            .as_ref()
            .map_or(0, |private| private.spool.active_count())
    }
}

fn cleanup_private_ids(private: &mut PrivateSpool, ids: Vec<String>, now_ms: u64) {
    for artifact_id in ids {
        private.descriptor_ready.remove(&artifact_id);
        private.input_paths_issued.remove(&artifact_id);
        if let Some(binding) = private.bindings.remove(&artifact_id) {
            let _ = private.spool.abort(&binding, now_ms);
        }
    }
}

fn exact_private_binding(binding: &ArtifactBinding, expected: &Binding) -> bool {
    binding.route().bridge_id() == expected.bridge_id
        && binding.route().load_generation_id() == expected.load_generation_id
        && binding.context_id() == expected.context_id
        && binding.browser_session_id() == expected.browser_session_id
        && binding.turn_id() == expected.turn_id
        && binding.action_id() == expected.action_id
        && binding.op_id() == expected.op_id
        && binding.artifact_id() == expected.artifact_id
        && binding.direction() == ArtifactDirection::Output
        && binding.purpose().as_str() == expected.purpose
}

fn exact_operation_binding(binding: &ArtifactBinding, expected: &PendingOperationBinding) -> bool {
    binding.route().bridge_id() == expected.bridge_id
        && binding.route().load_generation_id() == expected.load_generation_id
        && binding.context_id() == expected.context_id
        && binding.browser_session_id() == expected.browser_session_id
        && binding.turn_id() == expected.turn_id
        && binding.action_id() == expected.action_id
        && binding.op_id() == expected.op_id
        && binding.purpose().as_str() == expected.action
}

fn descriptor_matches_fields(
    descriptor: &crate::artifact::ArtifactDescriptor,
    fields: &BTreeMap<String, Value>,
) -> bool {
    fields.get("artifact_id").and_then(Value::as_str) == Some(descriptor.artifact_id())
        && fields.get("mime_type").and_then(Value::as_str) == Some(descriptor.mime_type())
        && fields.get("byte_count").and_then(Value::as_u64) == Some(descriptor.byte_count())
        && fields.get("sha256").and_then(Value::as_str) == Some(descriptor.sha256())
        && fields.get("purpose").and_then(Value::as_str) == Some(descriptor.purpose().as_str())
}

fn descriptor_matches_claim(
    descriptor: &crate::artifact::ArtifactDescriptor,
    claim: &VerifiedArtifactClaim,
) -> bool {
    descriptor.artifact_id() == claim.artifact_id
        && descriptor.mime_type() == claim.mime_type
        && descriptor.byte_count() == claim.byte_count
        && descriptor.sha256() == claim.sha256
        && descriptor.purpose().as_str() == claim.purpose
}

fn map_spool_error(error: ArtifactError) -> ArtifactCodecError {
    match error {
        ArtifactError::PrivateSpoolUnavailable => ArtifactCodecError::PrivateSpoolUnavailable,
        ArtifactError::RegistryFull | ArtifactError::SpoolFull => ArtifactCodecError::Capacity,
        ArtifactError::ClockInvalid | ArtifactError::ClockInvalidAndAborted => {
            ArtifactCodecError::ClockInvalid
        }
        _ => ArtifactCodecError::InvalidPacket,
    }
}

fn artifact_phase(method: &str) -> Result<&'static str, ArtifactCodecError> {
    match method {
        "artifact.begin" => Ok("begin"),
        "artifact.chunk" => Ok("chunk"),
        "artifact.end" => Ok("end"),
        "artifact.abort" => Ok("abort"),
        _ => Err(ArtifactCodecError::UnsupportedEvent),
    }
}

fn binding(
    fields: &BTreeMap<String, Value>,
    operation: &PendingOperationBinding,
) -> Result<Binding, ArtifactCodecError> {
    let candidate = Binding {
        bridge_id: operation.bridge_id.clone(),
        load_generation_id: operation.load_generation_id.clone(),
        context_id: identifier(fields, "context_id")?.to_owned(),
        browser_session_id: identifier(fields, "browser_session_id")?.to_owned(),
        turn_id: identifier(fields, "turn_id")?.to_owned(),
        action_id: identifier(fields, "action_id")?.to_owned(),
        op_id: identifier(fields, "op_id")?.to_owned(),
        artifact_id: identifier(fields, "artifact_id")?.to_owned(),
        purpose: fields
            .get("purpose")
            .and_then(Value::as_str)
            .ok_or(ArtifactCodecError::InvalidPacket)?
            .to_owned(),
    };
    if fields.get("direction").and_then(Value::as_str) != Some("output")
        || operation.action != candidate.purpose
        || operation.context_id != candidate.context_id
        || operation.browser_session_id != candidate.browser_session_id
        || operation.turn_id != candidate.turn_id
        || operation.action_id != candidate.action_id
        || operation.op_id != candidate.op_id
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    Ok(candidate)
}

fn binding_from_ack(
    fields: &BTreeMap<String, Value>,
    credential_bridge_id: &str,
) -> Result<Binding, ArtifactCodecError> {
    if integer(fields, "contract_version")? != 1
        || identifier(fields, "bridge_id")? != credential_bridge_id
        || fields.get("direction").and_then(Value::as_str) != Some("output")
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    Ok(Binding {
        bridge_id: identifier(fields, "bridge_id")?.to_owned(),
        load_generation_id: identifier(fields, "load_generation_id")?.to_owned(),
        context_id: identifier(fields, "context_id")?.to_owned(),
        browser_session_id: identifier(fields, "browser_session_id")?.to_owned(),
        turn_id: identifier(fields, "turn_id")?.to_owned(),
        action_id: identifier(fields, "action_id")?.to_owned(),
        op_id: identifier(fields, "op_id")?.to_owned(),
        artifact_id: identifier(fields, "artifact_id")?.to_owned(),
        purpose: fields
            .get("purpose")
            .and_then(Value::as_str)
            .filter(|value| matches!(*value, "screenshot" | "download"))
            .ok_or(ArtifactCodecError::InvalidPacket)?
            .to_owned(),
    })
}

fn core_packet(
    fields: &BTreeMap<String, Value>,
    binding: &Binding,
    phase: &str,
) -> Result<String, ArtifactCodecError> {
    let mut data = fields.clone();
    data.insert("phase".into(), Value::String(phase.into()));
    data.insert("bridge_id".into(), Value::String(binding.bridge_id.clone()));
    data.insert(
        "load_generation_id".into(),
        Value::String(binding.load_generation_id.clone()),
    );
    if let Some(encoded) = data.remove("data") {
        data.insert("data_base64".into(), encoded);
    }
    let packet = format!(
        "42/ws,{}",
        Value::Array(vec![
            Value::String("connector_browser_artifact_chunk".into()),
            Value::Object(BTreeMap::from([
                ("correlationId".into(), Value::String(binding.op_id.clone()),),
                ("data".into(), Value::Object(data)),
            ])),
        ])
        .encode()
    );
    if packet.len() > rpc::MAX_NATIVE_FRAME_BYTES {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    Ok(packet)
}

fn validate_envelope(
    fields: &BTreeMap<String, Value>,
    expected_handler_id: &str,
) -> Result<(), ArtifactCodecError> {
    if fields.get("handlerId").and_then(Value::as_str) != Some(expected_handler_id)
        || !fields.contains_key("correlationId")
        || !fields.contains_key("data")
        || fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "handlerId" | "eventId" | "correlationId" | "ts" | "data"
            )
        })
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    if fields
        .get("eventId")
        .is_some_and(|value| !value.as_str().is_some_and(rpc::valid_opaque_id))
        || fields.get("ts").is_some_and(|value| {
            !value.as_str().is_some_and(|value| {
                !value.is_empty() && value.len() <= 64 && !value.chars().any(char::is_control)
            })
        })
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    Ok(())
}

fn validate_ack_shape(
    fields: &BTreeMap<String, Value>,
    phase: &str,
) -> Result<(), ArtifactCodecError> {
    if !matches!(phase, "begin" | "chunk" | "end" | "abort") {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    let status = fields
        .get("status")
        .and_then(Value::as_str)
        .ok_or(ArtifactCodecError::InvalidPacket)?;
    let additional: &[&str] = match status {
        "accepted" | "duplicate" if matches!(phase, "begin" | "chunk") => {
            &["status", "next_chunk_index", "received_bytes"]
        }
        "complete" if phase == "end" => &["status", "descriptor"],
        "aborted" => &["status", "reason_code"],
        _ => return Err(ArtifactCodecError::InvalidPacket),
    };
    if fields.len() != CORE_COMMON_KEYS.len() + additional.len()
        || CORE_COMMON_KEYS
            .iter()
            .chain(additional)
            .any(|key| !fields.contains_key(*key))
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    if matches!(status, "accepted" | "duplicate")
        && (integer(fields, "next_chunk_index")? > MAX_SAFE_INTEGER
            || integer(fields, "received_bytes")? > rpc::MAX_ARTIFACT_BYTES)
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    if status == "aborted"
        && !fields
            .get("reason_code")
            .and_then(Value::as_str)
            .is_some_and(valid_abort_reason)
    {
        return Err(ArtifactCodecError::InvalidPacket);
    }
    Ok(())
}

fn validate_descriptor(
    fields: &BTreeMap<String, Value>,
    transfer: &Transfer,
) -> Result<(), ArtifactCodecError> {
    let descriptor = fields
        .get("descriptor")
        .and_then(Value::as_object)
        .ok_or(ArtifactCodecError::InvalidResponse)?;
    const KEYS: &[&str] = &[
        "artifact_id",
        "mime_type",
        "byte_count",
        "sha256",
        "purpose",
    ];
    if descriptor.len() != KEYS.len()
        || KEYS.iter().any(|key| !descriptor.contains_key(*key))
        || descriptor.get("artifact_id").and_then(Value::as_str)
            != Some(&transfer.binding.artifact_id)
        || descriptor.get("mime_type").and_then(Value::as_str) != Some(&transfer.mime_type)
        || descriptor.get("byte_count").and_then(Value::as_u64) != Some(transfer.byte_count)
        || descriptor.get("sha256").and_then(Value::as_str) != Some(&transfer.sha256)
        || descriptor.get("purpose").and_then(Value::as_str) != Some(&transfer.binding.purpose)
    {
        return Err(ArtifactCodecError::InvalidResponse);
    }
    Ok(())
}

fn valid_abort_reason(value: &str) -> bool {
    matches!(
        value,
        "ARTIFACT_TOO_LARGE"
            | "CANCELED"
            | "CONNECTION_LOST"
            | "DEADLINE_EXCEEDED"
            | "INTERNAL_ERROR"
            | "OUTCOME_UNKNOWN"
            | "ARTIFACT_ALREADY_COMPLETE"
            | "ARTIFACT_DIGEST_INVALID"
            | "ARTIFACT_ID_REUSED"
            | "ARTIFACT_INTEGRITY_MISMATCH"
            | "ARTIFACT_MIME_INVALID"
            | "ARTIFACT_NOT_COMPLETE"
            | "ARTIFACT_NOT_FOUND"
            | "ARTIFACT_REGISTRY_FULL"
            | "ARTIFACT_SIZE_INVALID"
            | "ARTIFACT_SIZE_MISMATCH"
            | "ARTIFACT_SPOOL_FULL"
            | "ARTIFACT_TERMINAL"
            | "CHUNK_OUT_OF_ORDER"
            | "CHUNK_SIZE_INVALID"
            | "CLOCK_UNAVAILABLE"
            | "IDEMPOTENCY_CONFLICT"
            | "SPOOL_UNAVAILABLE"
    )
}

fn parse_sha256(value: &str) -> Result<[u8; 32], ArtifactCodecError> {
    let digest = value
        .strip_prefix("sha256:")
        .filter(|value| value.len() == 64)
        .ok_or(ArtifactCodecError::InvalidPacket)?;
    let mut output = [0_u8; 32];
    for (index, pair) in digest.as_bytes().chunks_exact(2).enumerate() {
        let high = hex(pair[0]).ok_or(ArtifactCodecError::InvalidPacket)?;
        let low = hex(pair[1]).ok_or(ArtifactCodecError::InvalidPacket)?;
        output[index] = (high << 4) | low;
    }
    Ok(output)
}

fn hex(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        _ => None,
    }
}

fn identifier<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a str, ArtifactCodecError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| rpc::valid_opaque_id(value))
        .ok_or(ArtifactCodecError::InvalidPacket)
}

fn integer(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, ArtifactCodecError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(ArtifactCodecError::InvalidPacket)
}

fn integer_response(
    fields: &BTreeMap<String, Value>,
    key: &str,
) -> Result<u64, ArtifactCodecError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(ArtifactCodecError::InvalidResponse)
}

fn frame_key(binding: &Binding, phase: &str) -> String {
    [
        binding.bridge_id.as_str(),
        binding.load_generation_id.as_str(),
        binding.context_id.as_str(),
        binding.browser_session_id.as_str(),
        binding.turn_id.as_str(),
        binding.action_id.as_str(),
        binding.op_id.as_str(),
        binding.artifact_id.as_str(),
        binding.purpose.as_str(),
        phase,
    ]
    .join("\0")
}

fn timeout_response(id: &str) -> Vec<u8> {
    RpcMessage::Response(RpcResponse {
        id: id.to_owned(),
        result: Err(RpcErrorObject {
            code: -32_010,
            message: "The artifact transfer outcome is unknown.".into(),
            data: Some(Value::Object(BTreeMap::from([
                ("a0_code".into(), Value::String("OUTCOME_UNKNOWN".into())),
                ("outcome".into(), Value::String("unknown".into())),
                ("retryable".into(), Value::Bool(false)),
                ("details".into(), Value::Object(BTreeMap::new())),
            ]))),
        }),
    })
    .encode()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native_host::NativeInvocation;
    use std::sync::Arc;

    const BRIDGE: &str = "bridge-1";
    const GENERATION: &str = "generation-1";

    fn operation_with_native_id_and_profile(
        action: &str,
        transport_profile: BrowserTransportProfile,
    ) -> (ConnectorCodec, String) {
        let RpcMessage::Request(mut request) = rpc::parse_message(
            include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json"),
            Peer::Server,
        )
        .unwrap() else {
            panic!()
        };
        let Value::Object(fields) = &mut request.params else {
            panic!()
        };
        fields.insert("action".into(), Value::String(action.into()));
        if action == "upload_file" {
            fields.insert(
                "args".into(),
                Value::Object(BTreeMap::from([
                    ("ref".into(), Value::String("frame0:node24".into())),
                    (
                        "expected_action_class".into(),
                        Value::String("external_side_effect".into()),
                    ),
                    ("artifact_id".into(), Value::String("artifact-1".into())),
                    ("mime_type".into(), Value::String("text/plain".into())),
                    ("byte_count".into(), Value::Number("3".into())),
                    ("sha256".into(), Value::String(sha256(b"abc"))),
                ])),
            );
            fields.insert(
                "required_capabilities".into(),
                Value::Array(
                    [
                        "upload_file",
                        "artifacts_v1",
                        "trusted_input_v1",
                        "semantic_dom_v1",
                    ]
                    .into_iter()
                    .map(|value| Value::String(value.into()))
                    .collect(),
                ),
            );
        }
        fields.insert("op_id".into(), Value::String("op-1".into()));
        fields.insert("bridge_id".into(), Value::String(BRIDGE.into()));
        fields.insert(
            "load_generation_id".into(),
            Value::String(GENERATION.into()),
        );
        let packet = format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_op".into()),
                Value::Object(BTreeMap::from([
                    (
                        "handlerId".into(),
                        Value::String(transport_profile.handler_id().into()),
                    ),
                    ("correlationId".into(), Value::String("op-1".into())),
                    ("data".into(), request.params),
                ])),
            ])
            .encode()
        );
        let mut operations = ConnectorCodec::with_profile(GENERATION.into(), transport_profile);
        let native = operations.command(&packet, BRIDGE).unwrap();
        let RpcMessage::Request(native) = rpc::parse_message(&native, Peer::Server).unwrap() else {
            panic!()
        };
        (operations, native.id.unwrap())
    }

    fn operation_with_native_id(action: &str) -> (ConnectorCodec, String) {
        operation_with_native_id_and_profile(action, BrowserTransportProfile::compiled())
    }

    fn operation(action: &str) -> ConnectorCodec {
        operation_with_native_id(action).0
    }

    fn request(
        method: &str,
        id: &str,
        purpose: &str,
        additional: impl IntoIterator<Item = (&'static str, Value)>,
    ) -> RpcRequest {
        let mut fields = BTreeMap::from([
            ("contract_version".into(), Value::Number("1".into())),
            ("context_id".into(), Value::String("context-1".into())),
            (
                "browser_session_id".into(),
                Value::String("browser-session-1".into()),
            ),
            ("turn_id".into(), Value::String("turn-1".into())),
            ("action_id".into(), Value::String("action-1".into())),
            ("op_id".into(), Value::String("op-1".into())),
            ("artifact_id".into(), Value::String("artifact-1".into())),
            ("direction".into(), Value::String("output".into())),
            ("purpose".into(), Value::String(purpose.into())),
        ]);
        fields.extend(
            additional
                .into_iter()
                .map(|(key, value)| (key.to_owned(), value)),
        );
        let encoded = RpcMessage::Request(RpcRequest {
            id: Some(id.into()),
            method: method.into(),
            params: Value::Object(fields),
        })
        .encode();
        let RpcMessage::Request(request) = rpc::parse_message(&encoded, Peer::Extension).unwrap()
        else {
            panic!()
        };
        request
    }

    fn sha256(bytes: &[u8]) -> String {
        let digest = Sha256::digest(bytes);
        let suffix = digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        format!("sha256:{suffix}")
    }

    fn ack(
        outbound: &[u8],
        status: &str,
        extra: impl IntoIterator<Item = (&'static str, Value)>,
    ) -> String {
        let packet = std::str::from_utf8(outbound).unwrap();
        let array = json::parse(packet.strip_prefix("42/ws,").unwrap().as_bytes()).unwrap();
        let envelope = array.as_array().unwrap()[1].as_object().unwrap();
        let mut data = envelope.get("data").unwrap().as_object().unwrap().clone();
        for key in [
            "mime_type",
            "byte_count",
            "sha256",
            "chunk_index",
            "data_base64",
            "reason_code",
        ] {
            data.remove(key);
        }
        data.insert("status".into(), Value::String(status.into()));
        data.extend(
            extra
                .into_iter()
                .map(|(key, value)| (key.to_owned(), value)),
        );
        format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_artifact_ack".into()),
                Value::Object(BTreeMap::from([
                    ("handlerId".into(), Value::String(HANDLER_ID.into())),
                    ("correlationId".into(), Value::String("op-1".into())),
                    ("data".into(), Value::Object(data)),
                ])),
            ])
            .encode()
        )
    }

    fn begin(payload: &[u8]) -> RpcRequest {
        request(
            "artifact.begin",
            "artifact-rpc-1",
            "screenshot",
            [
                ("mime_type", Value::String("image/png".into())),
                ("byte_count", Value::Number(payload.len().to_string())),
                ("sha256", Value::String(sha256(payload))),
            ],
        )
    }

    #[test]
    fn exact_output_transfer_maps_chunks_and_pathless_descriptor() {
        let payload = b"bounded screenshot";
        let operations = operation("screenshot");
        let mut codec = ArtifactCodec::new(0);

        let begin_packet = codec.request(&begin(payload), &operations, 1).unwrap();
        let begin_text = std::str::from_utf8(&begin_packet).unwrap();
        assert!(begin_text.contains("connector_browser_artifact_chunk"));
        assert!(begin_text.contains("\"bridge_id\":\"bridge-1\""));
        let begin_ack = ack(
            &begin_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("0".into())),
                ("received_bytes", Value::Number("0".into())),
            ],
        );
        let response = codec
            .acknowledgement(&begin_ack, BRIDGE, 2)
            .unwrap()
            .unwrap();
        let response = std::str::from_utf8(&response).unwrap();
        assert!(response.contains("\"id\":\"artifact-rpc-1\""));
        assert!(!response.contains("bridge_id"));
        assert!(!response.contains("load_generation_id"));
        assert_eq!(codec.active_count(), 1);

        let chunk = request(
            "artifact.chunk",
            "artifact-rpc-2",
            "screenshot",
            [
                ("chunk_index", Value::Number("0".into())),
                ("data", Value::String(STANDARD.encode(payload))),
            ],
        );
        let chunk_packet = codec.request(&chunk, &operations, 3).unwrap();
        let chunk_text = std::str::from_utf8(&chunk_packet).unwrap();
        assert!(chunk_text.contains("data_base64"));
        assert!(!chunk_text.contains("\"data\":\""));
        let chunk_ack = ack(
            &chunk_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("1".into())),
                ("received_bytes", Value::Number(payload.len().to_string())),
            ],
        );
        codec
            .acknowledgement(&chunk_ack, BRIDGE, 4)
            .unwrap()
            .unwrap();

        let end = request("artifact.end", "artifact-rpc-3", "screenshot", []);
        let end_packet = codec.request(&end, &operations, 5).unwrap();
        let descriptor = Value::Object(BTreeMap::from([
            ("artifact_id".into(), Value::String("artifact-1".into())),
            ("mime_type".into(), Value::String("image/png".into())),
            (
                "byte_count".into(),
                Value::Number(payload.len().to_string()),
            ),
            ("sha256".into(), Value::String(sha256(payload))),
            ("purpose".into(), Value::String("screenshot".into())),
        ]));
        let end_ack = ack(&end_packet, "complete", [("descriptor", descriptor)]);
        let response = codec.acknowledgement(&end_ack, BRIDGE, 6).unwrap().unwrap();
        assert!(std::str::from_utf8(&response)
            .unwrap()
            .contains("\"status\":\"complete\""));
        assert_eq!(codec.active_count(), 0);
        assert!(codec
            .acknowledgement(&end_ack, BRIDGE, 7)
            .unwrap()
            .is_none());
    }

    #[test]
    fn artifact_mapping_requires_one_exact_transport_profile() {
        let production = BrowserTransportProfile::fixture_production();
        let development = BrowserTransportProfile::fixture_development();
        let payload = b"abc";

        let production_operations =
            operation_with_native_id_and_profile("screenshot", production).0;
        let mut development_codec = ArtifactCodec::with_profile(0, development);
        assert_eq!(
            development_codec.request(&begin(payload), &production_operations, 1),
            Err(ArtifactCodecError::InvalidPacket)
        );

        let mut production_codec = ArtifactCodec::with_profile(0, production);
        let begin_packet = production_codec
            .request(&begin(payload), &production_operations, 2)
            .unwrap();
        let production_ack = ack(
            &begin_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("0".into())),
                ("received_bytes", Value::Number("0".into())),
            ],
        );
        let development_ack =
            production_ack.replace(production.handler_id(), development.handler_id());
        assert_eq!(
            production_codec.acknowledgement(&development_ack, BRIDGE, 3),
            Err(ArtifactCodecError::InvalidPacket)
        );
        assert!(production_codec
            .acknowledgement(&production_ack, BRIDGE, 4)
            .unwrap()
            .is_some());
    }

    #[test]
    fn pending_operation_and_chunk_progress_are_exactly_bound() {
        let payload = b"abc";
        let operations = operation("screenshot");
        let mut codec = ArtifactCodec::new(0);
        let wrong_purpose = begin(payload);
        let Value::Object(mut wrong_fields) = wrong_purpose.params.clone() else {
            panic!()
        };
        wrong_fields.insert("purpose".into(), Value::String("download".into()));
        let wrong_purpose = RpcRequest {
            params: Value::Object(wrong_fields),
            ..wrong_purpose
        };
        assert_eq!(
            codec.request(&wrong_purpose, &operations, 1),
            Err(ArtifactCodecError::InvalidPacket)
        );

        let begin_packet = codec.request(&begin(payload), &operations, 2).unwrap();
        let begin_ack = ack(
            &begin_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("0".into())),
                ("received_bytes", Value::Number("0".into())),
            ],
        );
        codec
            .acknowledgement(&begin_ack, BRIDGE, 3)
            .unwrap()
            .unwrap();
        let wrong_index = request(
            "artifact.chunk",
            "artifact-rpc-2",
            "screenshot",
            [
                ("chunk_index", Value::Number("1".into())),
                ("data", Value::String(STANDARD.encode(payload))),
            ],
        );
        assert_eq!(
            codec.request(&wrong_index, &operations, 4),
            Err(ArtifactCodecError::InvalidPacket)
        );

        let abort = request(
            "artifact.abort",
            "artifact-rpc-3",
            "screenshot",
            [("reason_code", Value::String("CANCELED".into()))],
        );
        let abort_packet = codec.request(&abort, &operations, 5).unwrap();
        let abort_ack = ack(
            &abort_packet,
            "aborted",
            [("reason_code", Value::String("CANCELED".into()))],
        );
        let response = codec
            .acknowledgement(&abort_ack, BRIDGE, 6)
            .unwrap()
            .unwrap();
        assert!(std::str::from_utf8(&response)
            .unwrap()
            .contains("\"status\":\"aborted\""));
        assert_eq!(codec.active_count(), 0);
    }

    #[test]
    fn mismatched_ack_fails_closed_and_cleans_the_pending_frame() {
        let payload = b"abc";
        let operations = operation("screenshot");
        let mut codec = ArtifactCodec::new(0);
        let begin_packet = codec.request(&begin(payload), &operations, 1).unwrap();
        let valid = ack(
            &begin_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("0".into())),
                ("received_bytes", Value::Number("0".into())),
            ],
        );
        assert_eq!(
            codec.acknowledgement(&valid, "other-bridge", 2),
            Err(ArtifactCodecError::InvalidPacket)
        );
        let mismatch = valid.replace("\"received_bytes\":0", "\"received_bytes\":1");
        assert_eq!(
            codec.acknowledgement(&mismatch, BRIDGE, 3),
            Err(ArtifactCodecError::InvalidResponse)
        );

        assert!(codec.expire(FRAME_TIMEOUT_MS + 1).unwrap().is_empty());
        assert!(codec
            .acknowledgement(&valid, BRIDGE, FRAME_TIMEOUT_MS + 2)
            .unwrap()
            .is_none());
    }

    #[cfg(unix)]
    struct FixtureParent(std::path::PathBuf);

    #[cfg(unix)]
    impl FixtureParent {
        fn new() -> Self {
            use std::os::unix::fs::PermissionsExt;

            let mut random = [0_u8; 16];
            getrandom::fill(&mut random).unwrap();
            let suffix = random
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>();
            let path = std::env::temp_dir().join(format!("a0-codec-spool-{suffix}"));
            std::fs::create_dir(&path).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
            Self(path)
        }
    }

    #[cfg(unix)]
    impl Drop for FixtureParent {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(unix)]
    fn private_codec(started_at_ms: u64) -> (ArtifactCodec, FixtureParent) {
        let invocation =
            NativeInvocation::fixture("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/");
        let route = ArtifactRoute::from_validated_invocation(
            &invocation,
            "install-1",
            GENERATION,
            "server-1",
            BRIDGE,
            1,
            "sid-1",
        )
        .unwrap();
        let expected = route.clone();
        let authorizer: CurrentRouteAuthorizer =
            Arc::new(move |binding| binding.route() == &expected);
        let parent = FixtureParent::new();
        let mut codec = ArtifactCodec::new(started_at_ms);
        codec
            .attach_private_spool(route, authorizer, started_at_ms, Some(&parent.0))
            .unwrap();
        (codec, parent)
    }

    #[cfg(all(unix, not(feature = "local-development")))]
    #[test]
    fn input_frames_verify_private_spool_and_exact_pending_upload_before_ack() {
        fn input(phase: &str, extra: impl IntoIterator<Item = (&'static str, Value)>) -> String {
            let request = request("artifact.end", "input-rpc", "screenshot", []);
            let mut fields = request.params.as_object().unwrap().clone();
            fields.insert("direction".into(), Value::String("input".into()));
            fields.insert("purpose".into(), Value::String("upload_file".into()));
            fields.insert("phase".into(), Value::String(phase.into()));
            fields.insert("bridge_id".into(), Value::String(BRIDGE.into()));
            fields.insert(
                "load_generation_id".into(),
                Value::String(GENERATION.into()),
            );
            fields.extend(extra.into_iter().map(|(key, value)| (key.into(), value)));
            format!(
                "42/ws,{}",
                Value::Array(vec![
                    Value::String("connector_browser_artifact_chunk".into()),
                    Value::Object(BTreeMap::from([
                        ("handlerId".into(), Value::String(HANDLER_ID.into())),
                        ("correlationId".into(), Value::String("artifact-1".into())),
                        ("data".into(), Value::Object(fields)),
                    ])),
                ])
                .encode()
            )
        }
        let operations = operation("upload_file");
        let (mut codec, _parent) = private_codec(0);
        let begin = input(
            "begin",
            [
                ("mime_type", Value::String("text/plain".into())),
                ("byte_count", Value::Number("3".into())),
                ("sha256", Value::String(sha256(b"abc"))),
            ],
        );
        assert!(codec
            .input_packet(&begin, "other-bridge", &operations, 1)
            .is_err());
        let ack = codec.input_packet(&begin, BRIDGE, &operations, 2).unwrap();
        assert!(std::str::from_utf8(&ack)
            .unwrap()
            .contains("connector_browser_artifact_ack"));
        assert!(codec.input_packet(&begin, BRIDGE, &operations, 3).is_err());
        let chunk = input(
            "chunk",
            [
                ("chunk_index", Value::Number("0".into())),
                ("data_base64", Value::String(STANDARD.encode(b"abc"))),
            ],
        );
        codec.input_packet(&chunk, BRIDGE, &operations, 4).unwrap();
        let end = input("end", []);
        let ack = codec.input_packet(&end, BRIDGE, &operations, 5).unwrap();
        let text = std::str::from_utf8(&ack).unwrap();
        assert!(text.contains("\"status\":\"complete\""));
        assert!(text.contains(&sha256(b"abc")));
        assert!(codec.input_packet(&end, BRIDGE, &operations, 6).is_err());
        assert_eq!(codec.private_active_count(), 1);
        let mut path_request = request("artifact.end", "path-request", "screenshot", []);
        path_request.method = "artifact.input_path".into();
        let fields = match &mut path_request.params {
            Value::Object(fields) => fields,
            _ => panic!(),
        };
        fields.insert("direction".into(), Value::String("input".into()));
        fields.insert("purpose".into(), Value::String("upload_file".into()));
        let path_response = codec
            .input_path_response(&path_request, &operations, 7)
            .unwrap();
        let RpcMessage::Response(response) =
            rpc::parse_message(&path_response, Peer::Server).unwrap()
        else {
            panic!()
        };
        let result = response.result.unwrap();
        let path = result.as_object().unwrap()["ephemeral_path"]
            .as_str()
            .unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"abc");
        assert!(!text.contains(path));
        assert!(codec
            .input_path_response(&path_request, &operations, 8)
            .is_err());
    }

    #[cfg(unix)]
    fn complete_private_transfer(
        codec: &mut ArtifactCodec,
        operations: &ConnectorCodec,
        payload: &[u8],
    ) {
        let begin_packet = codec.request(&begin(payload), operations, 1).unwrap();
        assert_eq!(codec.private_active_count(), 1);
        let begin_ack = ack(
            &begin_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("0".into())),
                ("received_bytes", Value::Number("0".into())),
            ],
        );
        codec
            .acknowledgement(&begin_ack, BRIDGE, 2)
            .unwrap()
            .unwrap();
        let chunk = request(
            "artifact.chunk",
            "artifact-rpc-2",
            "screenshot",
            [
                ("chunk_index", Value::Number("0".into())),
                ("data", Value::String(STANDARD.encode(payload))),
            ],
        );
        let chunk_packet = codec.request(&chunk, operations, 3).unwrap();
        let chunk_ack = ack(
            &chunk_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("1".into())),
                ("received_bytes", Value::Number(payload.len().to_string())),
            ],
        );
        codec
            .acknowledgement(&chunk_ack, BRIDGE, 4)
            .unwrap()
            .unwrap();
        let end = request("artifact.end", "artifact-rpc-3", "screenshot", []);
        let end_packet = codec.request(&end, operations, 5).unwrap();
        let end_ack = ack(
            &end_packet,
            "complete",
            [("descriptor", descriptor(payload, &sha256(payload)))],
        );
        codec.acknowledgement(&end_ack, BRIDGE, 6).unwrap().unwrap();
        assert_eq!(codec.private_active_count(), 1);
    }

    fn descriptor(payload: &[u8], digest: &str) -> Value {
        Value::Object(BTreeMap::from([
            ("artifact_id".into(), Value::String("artifact-1".into())),
            ("mime_type".into(), Value::String("image/png".into())),
            (
                "byte_count".into(),
                Value::Number(payload.len().to_string()),
            ),
            ("sha256".into(), Value::String(digest.into())),
            ("purpose".into(), Value::String("screenshot".into())),
        ]))
    }

    fn screenshot_response(native_id: &str, payload: &[u8], digest: &str) -> Vec<u8> {
        RpcMessage::Response(RpcResponse {
            id: native_id.into(),
            result: Ok(Value::Object(BTreeMap::from([
                ("contract_version".into(), Value::Number("1".into())),
                ("op_id".into(), Value::String("op-1".into())),
                ("action_id".into(), Value::String("action-1".into())),
                ("status".into(), Value::String("succeeded".into())),
                (
                    "result".into(),
                    Value::Object(BTreeMap::from([
                        ("lease_id".into(), Value::String("lease-1".into())),
                        ("browser_id".into(), Value::String("browser-1".into())),
                        ("tab_handle".into(), Value::String("tab-1".into())),
                        ("artifact_id".into(), Value::String("artifact-1".into())),
                    ])),
                ),
                ("receipts".into(), Value::Array(Vec::new())),
                (
                    "artifacts".into(),
                    Value::Array(vec![descriptor(payload, digest)]),
                ),
            ]))),
        })
        .encode()
    }

    #[cfg(unix)]
    #[test]
    fn private_spool_verifies_before_forward_and_consumes_only_on_exact_op_settlement() {
        let payload = b"private screenshot";
        let (mut operations, native_id) = operation_with_native_id("screenshot");
        let (mut codec, _parent) = private_codec(0);
        complete_private_transfer(&mut codec, &operations, payload);

        let response = screenshot_response(&native_id, payload, &sha256(payload));
        let prepared = operations.prepare_response(&response).unwrap();
        codec
            .settle_operation(prepared.operation().unwrap(), 7)
            .unwrap();
        assert_eq!(codec.private_active_count(), 0);
        let packet = operations.commit_response(prepared).unwrap();
        assert!(packet.contains("connector_browser_op_result"));
        assert!(packet.contains("\"artifact_id\":\"artifact-1\""));
        assert!(!packet.contains("a0-codec-spool"));
    }

    #[cfg(unix)]
    #[test]
    fn failed_operation_aborts_the_exact_private_spool() {
        let payload = b"private screenshot";
        let (mut operations, native_id) = operation_with_native_id("screenshot");
        let (mut codec, _parent) = private_codec(0);
        let begin_packet = codec.request(&begin(payload), &operations, 1).unwrap();
        let begin_ack = ack(
            &begin_packet,
            "accepted",
            [
                ("next_chunk_index", Value::Number("0".into())),
                ("received_bytes", Value::Number("0".into())),
            ],
        );
        codec
            .acknowledgement(&begin_ack, BRIDGE, 2)
            .unwrap()
            .unwrap();
        assert_eq!(codec.private_active_count(), 1);
        let response = RpcMessage::Response(RpcResponse {
            id: native_id,
            result: Err(RpcErrorObject {
                code: -32_010,
                message: "redacted".into(),
                data: Some(Value::Object(BTreeMap::from([
                    ("a0_code".into(), Value::String("CANCELED".into())),
                    ("outcome".into(), Value::String("not_applied".into())),
                    ("retryable".into(), Value::Bool(false)),
                    ("details".into(), Value::Object(BTreeMap::new())),
                ]))),
            }),
        })
        .encode();
        let prepared = operations.prepare_response(&response).unwrap();
        codec
            .settle_operation(prepared.operation().unwrap(), 3)
            .unwrap();
        assert_eq!(codec.private_active_count(), 0);
        operations.commit_response(prepared).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn descriptor_mismatch_rejects_and_cleans_private_bytes() {
        let payload = b"private screenshot";
        let (operations, native_id) = operation_with_native_id("screenshot");
        let (mut codec, _parent) = private_codec(0);
        complete_private_transfer(&mut codec, &operations, payload);

        let wrong = format!("sha256:{}", "0".repeat(64));
        let response = screenshot_response(&native_id, payload, &wrong);
        let prepared = operations.prepare_response(&response).unwrap();
        assert!(codec
            .settle_operation(prepared.operation().unwrap(), 7)
            .is_err());
        assert_eq!(codec.private_active_count(), 0);
    }
}
