//! Pure native relay/session state machine.
//!
//! The production executable can construct a session only from a
//! `NativeInvocation` returned by the compiled-origin validator. Fixture
//! authority exists only under `cfg(test)`.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fmt::Write as _;
use std::sync::{atomic::AtomicBool, Arc};

use crate::artifact::ArtifactRoute;
use crate::artifact_codec::{ArtifactCodec, ArtifactCodecError};
use crate::connector_codec::ConnectorCodec;
use crate::context_codec::ContextCodec;
use crate::core_connector::{ConnectionStatus, CoreCommand, CoreConnection};
#[cfg(feature = "local-development")]
use crate::development_session::DevelopmentRuntimeRoute;
use crate::event_codec::{AckResponseDisposition, EventCodec, EventCodecError};
use crate::json::Value;
use crate::native_host::NativeInvocation;
use crate::pairing::{PairingFailure, PairingHello, PairingService};
use crate::rpc::{
    self, hello_extension_id, hello_install_instance_id, parse_message, request_timeout_ms, Peer,
    RpcErrorObject, RpcMessage, RpcRequest, RpcResponse, RpcValidationError,
};
use crate::runtime_handshake::{runtime_platform, AdmittedRuntimeRoute, ExtensionRuntimeHello};
use crate::transport_profile::BrowserTransportProfile;
use crate::COMPANION_VERSION;

pub const MAX_PENDING_CORRELATIONS: usize = 512;
pub const MAX_COMPLETED_CORRELATIONS: usize = 2_048;
#[cfg(feature = "local-development")]
const CORE_HELLO_FALLBACK_MS: u64 = crate::development_session::DEVELOPMENT_HELLO_FALLBACK_MS;
#[cfg(not(feature = "local-development"))]
const CORE_HELLO_FALLBACK_MS: u64 = rpc::HELLO_TIMEOUT_MS;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionState {
    AwaitingHello,
    Authenticating,
    PairingOnly,
    Ready,
    Blocked,
    Closed,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionError {
    Rpc(RpcValidationError),
    InvalidState,
    HelloRequired,
    HelloDeadlineExceeded,
    ExtensionIdentityMismatch,
    DuplicateCorrelation,
    UnknownCorrelation,
    CorrelationExpired,
    CorrelationCapacityExceeded,
    ClockInvalid,
    ConnectorPacketInvalid,
    ConnectorUnavailable,
    EntropyUnavailable,
}

impl SessionError {
    pub const fn reason_code(&self) -> &'static str {
        match self {
            Self::Rpc(error) => error.reason_code(),
            Self::InvalidState => "NATIVE_SESSION_INVALID_STATE",
            Self::HelloRequired => "NATIVE_HELLO_REQUIRED",
            Self::HelloDeadlineExceeded => "NATIVE_HELLO_DEADLINE_EXCEEDED",
            Self::ExtensionIdentityMismatch => "EXTENSION_IDENTITY_MISMATCH",
            Self::DuplicateCorrelation => "NATIVE_CORRELATION_DUPLICATE",
            Self::UnknownCorrelation => "NATIVE_CORRELATION_UNKNOWN",
            Self::CorrelationExpired => "DEADLINE_EXCEEDED",
            Self::CorrelationCapacityExceeded => "NATIVE_CORRELATION_CAPACITY_EXCEEDED",
            Self::ClockInvalid => "NATIVE_CLOCK_INVALID",
            Self::ConnectorPacketInvalid => "CONNECTOR_PACKET_INVALID",
            Self::ConnectorUnavailable => "CONNECTOR_CONNECTION_LOST",
            Self::EntropyUnavailable => "SECURE_ENTROPY_UNAVAILABLE",
        }
    }
}

impl From<RpcValidationError> for SessionError {
    fn from(error: RpcValidationError) -> Self {
        Self::Rpc(error)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RoutedMessage {
    pub target: Peer,
    pub payload: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct CorrelationKey {
    response_source: Peer,
    id: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PendingCorrelation {
    request_source: Peer,
    deadline_ms: u64,
    effect_possible: bool,
}

struct PendingNativeHello {
    id: String,
    connection_id: String,
    pairing: PairingHello,
}

struct DeferredUpload {
    rpc_id: String,
    artifact_id: String,
    payload: Vec<u8>,
}

pub struct RelaySession {
    transport_profile: BrowserTransportProfile,
    state: SessionState,
    invocation: NativeInvocation,
    caller_origin: String,
    caller_extension_id: String,
    install_instance_id: Option<String>,
    runtime_hello: Option<ExtensionRuntimeHello>,
    pending_native_hello: Option<PendingNativeHello>,
    admitted_route: Option<AdmittedRuntimeRoute>,
    #[cfg(feature = "local-development")]
    development_route: Option<DevelopmentRuntimeRoute>,
    started_at_ms: u64,
    last_now_ms: u64,
    pending: BTreeMap<CorrelationKey, PendingCorrelation>,
    completed: BTreeSet<CorrelationKey>,
    completed_order: VecDeque<CorrelationKey>,
    server_ready: bool,
    pairing: Arc<PairingService>,
    core_connection: Option<CoreConnection>,
    native_input_closed: Arc<AtomicBool>,
    core_connect_requested: bool,
    core_codec: Option<ConnectorCodec>,
    artifact_codec: Option<ArtifactCodec>,
    context_codec: ContextCodec,
    event_codec: Option<EventCodec>,
    deferred_uploads: BTreeMap<String, DeferredUpload>,
    #[cfg(test)]
    private_spool_parent: Option<std::path::PathBuf>,
}

impl RelaySession {
    pub fn from_validated_invocation(invocation: &NativeInvocation, started_at_ms: u64) -> Self {
        Self::with_server_state(invocation, started_at_ms, false)
    }

    fn with_server_state(
        invocation: &NativeInvocation,
        started_at_ms: u64,
        server_ready: bool,
    ) -> Self {
        let caller_origin = invocation.caller_origin().to_owned();
        let caller_extension_id = rpc::extension_id_from_origin(&caller_origin)
            .expect("validated invocation must contain an exact extension origin")
            .to_owned();
        #[cfg(not(test))]
        let pairing = PairingService::system();
        #[cfg(test)]
        let pairing = PairingService::fixture_unavailable();
        Self {
            transport_profile: BrowserTransportProfile::compiled(),
            state: SessionState::AwaitingHello,
            invocation: invocation.clone(),
            caller_origin,
            caller_extension_id,
            install_instance_id: None,
            runtime_hello: None,
            pending_native_hello: None,
            admitted_route: None,
            #[cfg(feature = "local-development")]
            development_route: None,
            started_at_ms,
            last_now_ms: started_at_ms,
            pending: BTreeMap::new(),
            completed: BTreeSet::new(),
            completed_order: VecDeque::new(),
            server_ready,
            pairing: Arc::new(pairing),
            core_connection: None,
            native_input_closed: Arc::new(AtomicBool::new(false)),
            core_connect_requested: false,
            core_codec: None,
            artifact_codec: None,
            context_codec: ContextCodec::new(),
            event_codec: None,
            deferred_uploads: BTreeMap::new(),
            #[cfg(test)]
            private_spool_parent: None,
        }
    }

    #[cfg(test)]
    pub(crate) fn fixture(caller_origin: &str, started_at_ms: u64) -> Self {
        let invocation = NativeInvocation::fixture(caller_origin);
        Self::with_server_state(&invocation, started_at_ms, true)
    }

    pub const fn state(&self) -> SessionState {
        self.state
    }

    pub(crate) fn native_input_closed_signal(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.native_input_closed)
    }

    pub fn caller_origin(&self) -> &str {
        &self.caller_origin
    }

    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    pub fn artifact_route(&self) -> Option<ArtifactRoute> {
        #[cfg(feature = "local-development")]
        {
            return None;
        }
        #[cfg(not(feature = "local-development"))]
        {
            if self.state != SessionState::Ready
                || self
                    .core_connection
                    .as_ref()
                    .is_some_and(|connection| connection.status() != ConnectionStatus::Ready)
            {
                return None;
            }
            self.admitted_route
                .as_ref()?
                .artifact_route(&self.invocation)
                .ok()
        }
    }

    pub fn receive(
        &mut self,
        source: Peer,
        payload: &[u8],
        now_ms: u64,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        self.observe_clock(now_ms)?;
        if matches!(self.state, SessionState::Blocked | SessionState::Closed) {
            return Err(SessionError::InvalidState);
        }

        let message = match parse_message(payload, source) {
            Ok(message) => message,
            Err(error) => {
                self.state = SessionState::Blocked;
                return Err(SessionError::Rpc(error));
            }
        };
        if self.state == SessionState::AwaitingHello {
            return self.receive_hello(source, message, now_ms);
        }
        if self.state == SessionState::Authenticating {
            return Err(SessionError::InvalidState);
        }
        if self.state == SessionState::PairingOnly {
            return self.receive_pairing_only(source, message, now_ms);
        }
        if !self.transport_profile.permits_context()
            && matches!(
                &message,
                RpcMessage::Request(request) if request.method.starts_with("context.") || request.method == "browser.approval_decision" || request.method.starts_with("credential.")
            )
        {
            return Err(SessionError::InvalidState);
        }
        #[cfg(feature = "local-development")]
        if matches!(
            &message,
            RpcMessage::Request(request) if request.method.starts_with("artifact.")
        ) {
            return Err(SessionError::InvalidState);
        }
        if source == Peer::Extension {
            if let RpcMessage::Request(request) = &message {
                if request.method == "pairing.disconnect" {
                    let id = request.id.as_deref().ok_or(SessionError::InvalidState)?;
                    let install_instance_id = self
                        .install_instance_id
                        .as_deref()
                        .ok_or(SessionError::InvalidState)?
                        .to_owned();

                    // Withdraw all exact runtime authority before touching the
                    // profile credential. The response is returned to Chrome
                    // first; Blocked then makes the native host close this port,
                    // so the disconnected profile cannot promote again here.
                    self.core_connection = None;
                    self.core_connect_requested = false;
                    self.pending_native_hello = None;
                    self.admitted_route = None;
                    #[cfg(feature = "local-development")]
                    {
                        self.development_route = None;
                    }
                    self.core_codec = None;
                    self.artifact_codec = None;
                    self.context_codec = ContextCodec::new();
                    self.event_codec = None;
                    self.pending.clear();
                    self.completed.clear();
                    self.completed_order.clear();
                    self.runtime_hello = None;
                    self.install_instance_id = None;
                    self.state = SessionState::Blocked;

                    return Ok(vec![RoutedMessage {
                        target: Peer::Extension,
                        payload: pairing_response(
                            id,
                            self.pairing
                                .disconnect(&self.caller_extension_id, &install_instance_id),
                        )
                        .encode(),
                    }]);
                }
                if request.method == "pairing.status" {
                    let id = request.id.as_deref().ok_or(SessionError::InvalidState)?;
                    let install_instance_id = self
                        .install_instance_id
                        .as_deref()
                        .ok_or(SessionError::InvalidState)?;
                    let mut status = self
                        .pairing
                        .status(&self.caller_extension_id, install_instance_id);
                    if let Some(connection) = &self.core_connection {
                        append_core_status(&mut status, connection.status());
                    }
                    return Ok(vec![RoutedMessage {
                        target: Peer::Extension,
                        payload: pairing_response(id, Ok(status)).encode(),
                    }]);
                }
            }
        }

        match message {
            RpcMessage::Request(request) if request.method == "bridge.hello" => {
                self.state = SessionState::Blocked;
                Err(SessionError::InvalidState)
            }
            RpcMessage::Request(request) if request.method == "bridge.ping" => {
                Ok(self.answer_ping(source, request, now_ms))
            }
            RpcMessage::Request(request) if request.method == "browser.event" => {
                self.forward_browser_event(source, request)
            }
            RpcMessage::Request(request)
                if source == Peer::Extension
                    && request.method == "credential.status"
                    && self
                        .pairing
                        .pending_rotation(
                            &self.caller_extension_id,
                            self.install_instance_id
                                .as_deref()
                                .ok_or(SessionError::InvalidState)?,
                        )
                        .map_err(|_| SessionError::ConnectorUnavailable)?
                        .is_none() =>
            {
                let id = request.id.as_deref().ok_or(SessionError::InvalidState)?;
                Ok(vec![RoutedMessage {
                    target: Peer::Extension,
                    payload: application_error(
                        id,
                        "NO_PENDING_KEY_UPDATE",
                        "There is no pending security key update.",
                        "not_applied",
                        false,
                    )
                    .encode(),
                }])
            }
            RpcMessage::Request(request)
                if source == Peer::Extension && request.method == "artifact.input_path" =>
            {
                let payload = self
                    .artifact_codec
                    .as_mut()
                    .ok_or(SessionError::InvalidState)?
                    .input_path_response(
                        &request,
                        self.core_codec.as_ref().ok_or(SessionError::InvalidState)?,
                        now_ms,
                    )
                    .map_err(map_artifact_codec_error)?;
                Ok(vec![RoutedMessage {
                    target: Peer::Extension,
                    payload,
                }])
            }
            RpcMessage::Request(request)
                if source == Peer::Extension && request.method.starts_with("artifact.") =>
            {
                self.forward_output_artifact(request, now_ms)
            }
            RpcMessage::Request(request) if request.method.starts_with("artifact.") => {
                Err(SessionError::InvalidState)
            }
            RpcMessage::Request(request) => self.forward_request(source, request, now_ms),
            RpcMessage::Response(response) if source == Peer::Extension => {
                let disposition = self
                    .event_codec
                    .as_mut()
                    .ok_or(SessionError::InvalidState)?
                    .ack_response(&response, now_ms)
                    .map_err(map_event_codec_error)?;
                match disposition {
                    AckResponseDisposition::Handled => Ok(Vec::new()),
                    AckResponseDisposition::NotOwned => {
                        self.forward_response(source, response, now_ms)
                    }
                }
            }
            RpcMessage::Response(response) => self.forward_response(source, response, now_ms),
        }
    }

    pub fn expire(&mut self, now_ms: u64) -> Result<Vec<RoutedMessage>, SessionError> {
        self.observe_clock(now_ms)?;
        if let Some(codec) = self.event_codec.as_mut() {
            codec.expire(now_ms);
        }
        let mut routed = self
            .artifact_codec
            .as_mut()
            .map(|codec| codec.expire(now_ms).map_err(map_artifact_codec_error))
            .transpose()?
            .unwrap_or_default()
            .into_iter()
            .map(|payload| RoutedMessage {
                target: Peer::Extension,
                payload,
            })
            .collect::<Vec<_>>();
        if self.state == SessionState::Authenticating
            && now_ms.saturating_sub(self.started_at_ms) >= CORE_HELLO_FALLBACK_MS
        {
            self.core_connection = None;
            routed.extend(self.finish_inactive_native_hello()?);
        }
        if self.state == SessionState::AwaitingHello
            && now_ms.saturating_sub(self.started_at_ms) >= rpc::HELLO_TIMEOUT_MS
        {
            self.state = SessionState::Blocked;
            return Err(SessionError::HelloDeadlineExceeded);
        }
        let expired = self
            .pending
            .iter()
            .filter(|(_, pending)| now_ms >= pending.deadline_ms)
            .map(|(key, pending)| (key.clone(), pending.clone()))
            .collect::<Vec<_>>();
        routed.reserve(expired.len());
        for (key, pending) in expired {
            self.deferred_uploads
                .retain(|_, upload| upload.rpc_id != key.id);
            if key.response_source == Peer::Server {
                self.context_codec.forget(&key.id);
            }
            self.pending.remove(&key);
            self.mark_completed(key.clone());
            routed.push(RoutedMessage {
                target: pending.request_source,
                payload: application_error(
                    &key.id,
                    if pending.effect_possible {
                        "OUTCOME_UNKNOWN"
                    } else {
                        "DEADLINE_EXCEEDED"
                    },
                    "The native relay request deadline expired.",
                    if pending.effect_possible {
                        "unknown"
                    } else {
                        "not_applied"
                    },
                    false,
                )
                .encode(),
            });
        }
        Ok(routed)
    }

    pub fn close(&mut self) {
        self.core_connection = None;
        self.core_connect_requested = false;
        self.install_instance_id = None;
        self.runtime_hello = None;
        self.pending_native_hello = None;
        self.admitted_route = None;
        #[cfg(feature = "local-development")]
        {
            self.development_route = None;
        }
        self.core_codec = None;
        self.artifact_codec = None;
        self.context_codec = ContextCodec::new();
        self.event_codec = None;
        self.pending.clear();
        self.deferred_uploads.clear();
        self.completed.clear();
        self.completed_order.clear();
        self.state = SessionState::Closed;
    }

    /// Start only after parsing an exact paired native hello. Unpaired setup
    /// replies are always flushed without starting Core network work.
    pub(crate) fn start_requested_core_connection(&mut self) {
        if !self.core_connect_requested {
            return;
        }
        self.core_connect_requested = false;
        #[cfg(not(test))]
        {
            let Some(extension_hello) = self.runtime_hello.clone() else {
                self.state = SessionState::Blocked;
                return;
            };
            self.core_connection = None;
            self.core_connection = Some(CoreConnection::start(
                Arc::clone(&self.pairing),
                extension_hello,
                Arc::clone(&self.native_input_closed),
            ));
        }
    }

    /// Drain a bounded number of commands; browser effects remain impossible in
    /// PairingOnly even if an authenticated peer sends a well-formed operation.
    pub(crate) fn poll_core(&mut self, now_ms: u64) -> Result<Vec<RoutedMessage>, SessionError> {
        let mut output = Vec::new();
        if self.state == SessionState::Authenticating {
            if self
                .core_connection
                .as_ref()
                .is_some_and(|connection| connection.transport_profile() != self.transport_profile)
            {
                self.core_connection = None;
                return Err(SessionError::ConnectorPacketInvalid);
            }
            let status = self
                .core_connection
                .as_ref()
                .map(CoreConnection::status)
                .unwrap_or(ConnectionStatus::Failed);
            if status == ConnectionStatus::AuthenticatedDevelopmentPairingOnly {
                // This is a signed, SID-bound development hello, not runtime
                // admission. Keep its heartbeat worker but answer Chrome with
                // the same paired-and-inactive native projection.
                output.extend(self.finish_inactive_native_hello()?);
            } else if status == ConnectionStatus::Ready {
                #[cfg(feature = "local-development")]
                {
                    let route = self
                        .core_connection
                        .as_ref()
                        .and_then(CoreConnection::take_development_route)
                        .ok_or(SessionError::ConnectorPacketInvalid)?;
                    if route.install_instance_id()
                        != self.install_instance_id.as_deref().unwrap_or("")
                        || route.load_generation_id()
                            != self
                                .runtime_hello
                                .as_ref()
                                .map(ExtensionRuntimeHello::load_generation_id)
                                .unwrap_or("")
                    {
                        self.core_connection = None;
                        return Err(SessionError::ConnectorPacketInvalid);
                    }
                    if self
                        .core_connection
                        .as_ref()
                        .is_none_or(|connection| connection.status() != ConnectionStatus::Ready)
                    {
                        self.core_connection = None;
                        return self.finish_inactive_native_hello();
                    }
                    let pending = self
                        .pending_native_hello
                        .take()
                        .ok_or(SessionError::InvalidState)?;
                    output.push(RoutedMessage {
                        target: Peer::Extension,
                        payload: hello_success(
                            &pending.id,
                            &pending.connection_id,
                            pending.pairing,
                            NativeHelloAdmission::Development(&route),
                            false,
                        )
                        .encode(),
                    });
                    self.development_route = Some(route);
                    self.state = SessionState::Ready;
                }
                #[cfg(not(feature = "local-development"))]
                {
                    let route = self
                        .core_connection
                        .as_ref()
                        .and_then(CoreConnection::take_admitted_route)
                        .ok_or(SessionError::ConnectorPacketInvalid)?;
                    if route.install_instance_id()
                        != self.install_instance_id.as_deref().unwrap_or("")
                        || route.load_generation_id()
                            != self
                                .runtime_hello
                                .as_ref()
                                .map(ExtensionRuntimeHello::load_generation_id)
                                .unwrap_or("")
                    {
                        self.core_connection = None;
                        return Err(SessionError::ConnectorPacketInvalid);
                    }
                    let artifact_route = match route.artifact_route(&self.invocation) {
                        Ok(route) => route,
                        Err(_) => {
                            self.core_connection = None;
                            return self.finish_inactive_native_hello();
                        }
                    };
                    let authorizer = self
                        .core_connection
                        .as_ref()
                        .ok_or(SessionError::ConnectorUnavailable)?
                        .artifact_authorizer(&artifact_route);
                    #[cfg(test)]
                    let spool_parent = self.private_spool_parent.clone();
                    #[cfg(not(test))]
                    let spool_parent: Option<std::path::PathBuf> = None;
                    if self
                        .artifact_codec
                        .as_mut()
                        .ok_or(SessionError::InvalidState)?
                        .attach_private_spool(
                            artifact_route,
                            authorizer,
                            now_ms,
                            spool_parent.as_deref(),
                        )
                        .is_err()
                    {
                        self.core_connection = None;
                        return self.finish_inactive_native_hello();
                    }
                    if self
                        .core_connection
                        .as_ref()
                        .is_none_or(|connection| connection.status() != ConnectionStatus::Ready)
                    {
                        self.core_connection = None;
                        self.artifact_codec = None;
                        return self.finish_inactive_native_hello();
                    }
                    let pending = self
                        .pending_native_hello
                        .take()
                        .ok_or(SessionError::InvalidState)?;
                    output.push(RoutedMessage {
                        target: Peer::Extension,
                        payload: hello_success(
                            &pending.id,
                            &pending.connection_id,
                            pending.pairing,
                            NativeHelloAdmission::Production(&route),
                            false,
                        )
                        .encode(),
                    });
                    self.admitted_route = Some(route);
                    self.state = SessionState::Ready;
                }
            } else if matches!(
                status,
                ConnectionStatus::Failed | ConnectionStatus::Stopped | ConnectionStatus::Unpaired
            ) {
                self.core_connection = None;
                output.extend(self.finish_inactive_native_hello()?);
            }
        }
        if self.state != SessionState::Ready {
            return Ok(output);
        }
        for _ in 0..8 {
            let Some(packet) = self
                .core_connection
                .as_ref()
                .and_then(CoreConnection::next_command)
            else {
                break;
            };
            self.validate_core_command_profile(&packet)?;
            let payload = if packet.packet.starts_with("43/ws,") {
                if !self.transport_profile.permits_context() {
                    return Err(SessionError::ConnectorPacketInvalid);
                }
                let Some(response) = self
                    .context_codec
                    .response(&packet.packet)
                    .map_err(|_| SessionError::ConnectorPacketInvalid)?
                else {
                    continue;
                };
                response
            } else {
                if self.state == SessionState::Ready {
                    #[cfg(not(feature = "local-development"))]
                    match self
                        .artifact_codec
                        .as_mut()
                        .ok_or(SessionError::InvalidState)?
                        .input_packet(
                            &packet.packet,
                            &packet.bridge_id,
                            self.core_codec.as_ref().ok_or(SessionError::InvalidState)?,
                            now_ms,
                        ) {
                        Ok(payload) => {
                            output.push(RoutedMessage {
                                target: Peer::Server,
                                payload,
                            });
                            output.extend(self.release_completed_uploads());
                            continue;
                        }
                        Err(ArtifactCodecError::UnsupportedEvent) => {}
                        Err(error) => return Err(map_artifact_codec_error(error)),
                    }
                    #[cfg(not(feature = "local-development"))]
                    match self
                        .artifact_codec
                        .as_mut()
                        .ok_or(SessionError::InvalidState)?
                        .acknowledgement(&packet.packet, &packet.bridge_id, now_ms)
                    {
                        Ok(Some(payload)) => {
                            output.push(RoutedMessage {
                                target: Peer::Extension,
                                payload,
                            });
                            continue;
                        }
                        Ok(None) => continue,
                        Err(ArtifactCodecError::UnsupportedEvent) => {}
                        Err(error) => return Err(map_artifact_codec_error(error)),
                    }
                    match self
                        .event_codec
                        .as_mut()
                        .ok_or(SessionError::InvalidState)?
                        .ack_command(&packet.packet, &packet.bridge_id, now_ms)
                    {
                        Ok(Some(payload)) => {
                            output.push(RoutedMessage {
                                target: Peer::Extension,
                                payload,
                            });
                            continue;
                        }
                        Ok(None) => continue,
                        Err(EventCodecError::UnsupportedEvent) => {}
                        Err(error) => return Err(map_event_codec_error(error)),
                    }
                }
                if self.transport_profile.permits_context() {
                    match self.context_codec.notification(&packet.packet) {
                        Ok(Some(notification)) => notification,
                        Ok(None) => continue,
                        Err(crate::connector_codec::CodecError::UnsupportedEvent) => self
                            .core_codec
                            .as_mut()
                            .ok_or(SessionError::InvalidState)?
                            .command(&packet.packet, &packet.bridge_id)
                            .map_err(|_| SessionError::ConnectorPacketInvalid)?,
                        Err(_) => return Err(SessionError::ConnectorPacketInvalid),
                    }
                } else {
                    self.core_codec
                        .as_mut()
                        .ok_or(SessionError::InvalidState)?
                        .command(&packet.packet, &packet.bridge_id)
                        .map_err(|_| SessionError::ConnectorPacketInvalid)?
                }
            };
            let retire = self.settle_credential_response(&payload)?;
            let messages = self.receive(Peer::Server, &payload, now_ms)?;
            for message in messages {
                if message.target == Peer::Extension && self.defer_upload(&message.payload)? {
                    continue;
                }
                output.push(message);
            }
            if retire {
                // Flush the acknowledgement before native host closes this port.
                // Reconnect constructs entirely fresh runtime authority.
                self.core_connection = None;
                self.admitted_route = None;
                self.state = SessionState::Blocked;
                break;
            }
        }
        if self.state == SessionState::Ready
            && self.core_connection.as_ref().is_some_and(|connection| {
                matches!(
                    connection.status(),
                    ConnectionStatus::Failed
                        | ConnectionStatus::Stopped
                        | ConnectionStatus::Unpaired
                        | ConnectionStatus::AuthenticatedRuntimePending
                        | ConnectionStatus::Connecting
                )
            })
        {
            return Err(SessionError::ConnectorUnavailable);
        }
        Ok(output)
    }

    pub(crate) fn send_core(&mut self, payload: &[u8]) -> Result<(), SessionError> {
        if std::str::from_utf8(payload).is_ok_and(|packet| {
            packet.starts_with("42/ws,[\"connector_browser_artifact_chunk\"")
                || packet.starts_with("42/ws,[\"connector_browser_artifact_ack\"")
        }) {
            #[cfg(feature = "local-development")]
            return Err(SessionError::InvalidState);
            #[cfg(not(feature = "local-development"))]
            return self
                .core_connection
                .as_ref()
                .ok_or(SessionError::ConnectorUnavailable)?
                .send_result(
                    std::str::from_utf8(payload)
                        .expect("validated UTF-8")
                        .to_owned(),
                )
                .map_err(|_| SessionError::ConnectorUnavailable);
        }
        let packet = match rpc::parse_message(payload, Peer::Extension)? {
            RpcMessage::Request(request) => {
                if !self.transport_profile.permits_context()
                    || !(request.method.starts_with("context.")
                        || request.method == "browser.approval_decision"
                        || request.method.starts_with("credential."))
                {
                    return Err(SessionError::InvalidState);
                }
                if request.method.starts_with("credential.") {
                    let fields = request
                        .params
                        .as_object()
                        .ok_or(SessionError::ConnectorPacketInvalid)?;
                    if fields.len() != 1
                        || fields.get("contract_version").and_then(Value::as_u64) != Some(1)
                    {
                        return Err(SessionError::ConnectorPacketInvalid);
                    }
                    let install = self
                        .install_instance_id
                        .as_deref()
                        .ok_or(SessionError::InvalidState)?;
                    let document = match request.method.as_str() {
                        "credential.rotate" => self
                            .pairing
                            .stage_rotation(&self.caller_extension_id, install)
                            .map_err(|_| SessionError::ConnectorUnavailable)?,
                        "credential.status" => {
                            let pending = self
                                .pairing
                                .pending_rotation(&self.caller_extension_id, install)
                                .map_err(|_| SessionError::ConnectorUnavailable)?
                                .ok_or(SessionError::InvalidState)?;
                            Value::Object(BTreeMap::from([
                                ("contract_version".into(), Value::Number("1".into())),
                                ("action".into(), Value::String("status".into())),
                                ("rotation_id".into(), Value::String(pending.rotation_id)),
                            ]))
                        }
                        "credential.revoke" => Value::Object(BTreeMap::from([
                            ("contract_version".into(), Value::Number("1".into())),
                            ("action".into(), Value::String("revoke".into())),
                        ])),
                        _ => return Err(SessionError::InvalidState),
                    };
                    self.context_codec.credential_request(payload, document)
                } else {
                    self.context_codec.request(payload)
                }
            }
            RpcMessage::Response(_) => {
                let prepared = self
                    .core_codec
                    .as_ref()
                    .ok_or(SessionError::InvalidState)?
                    .prepare_response(payload)
                    .map_err(|_| SessionError::ConnectorPacketInvalid)?;
                if let Some(operation) = prepared.operation() {
                    #[cfg(not(feature = "local-development"))]
                    self.artifact_codec
                        .as_mut()
                        .ok_or(SessionError::InvalidState)?
                        .settle_operation(operation, self.last_now_ms)
                        .map_err(map_artifact_codec_error)?;
                    #[cfg(feature = "local-development")]
                    if !operation.artifacts.is_empty() {
                        return Err(SessionError::ConnectorPacketInvalid);
                    }
                }
                self.core_codec
                    .as_mut()
                    .ok_or(SessionError::InvalidState)?
                    .commit_response(prepared)
            }
        }
        .map_err(|_| SessionError::ConnectorPacketInvalid)?;
        self.core_connection
            .as_ref()
            .ok_or(SessionError::ConnectorUnavailable)?
            .send_result(packet)
            .map_err(|_| SessionError::ConnectorUnavailable)
    }

    fn settle_credential_response(&self, payload: &[u8]) -> Result<bool, SessionError> {
        let data = match rpc::parse_message(payload, Peer::Server)? {
            RpcMessage::Response(RpcResponse {
                result: Ok(value), ..
            }) => value,
            RpcMessage::Request(request) if request.method == "credential.changed" => {
                request.params
            }
            _ => return Ok(false),
        };
        let Some(fields) = data.as_object() else {
            return Ok(false);
        };
        let Some(action) = fields.get("action").and_then(Value::as_str) else {
            return Ok(false);
        };
        if !matches!(action, "rotate" | "status" | "revoke") {
            return Ok(false);
        }
        let install = self
            .install_instance_id
            .as_deref()
            .ok_or(SessionError::InvalidState)?;
        let active = self
            .pairing
            .connector_credential(&self.caller_extension_id, install)
            .map_err(|_| SessionError::ConnectorUnavailable)?
            .ok_or(SessionError::InvalidState)?;
        let route = self
            .admitted_route
            .as_ref()
            .ok_or(SessionError::InvalidState)?;
        if active.bridge_id != route.bridge_id()
            || active.server_instance_id != route.server_instance_id()
            || active.key_generation() != route.key_generation()
        {
            return Err(SessionError::ConnectorPacketInvalid);
        }
        if fields.get("key_generation").and_then(Value::as_u64)
            != Some(u64::from(active.key_generation()))
        {
            return Err(SessionError::ConnectorPacketInvalid);
        }
        match fields.get("status").and_then(Value::as_str) {
            Some("pending") if action == "rotate" => Ok(true),
            Some("expired") => {
                let rotation = fields
                    .get("rotation_id")
                    .and_then(Value::as_str)
                    .ok_or(SessionError::ConnectorPacketInvalid)?;
                self.pairing
                    .expire_rotation(&self.caller_extension_id, install, rotation)
                    .map_err(|_| SessionError::ConnectorUnavailable)?;
                Ok(false)
            }
            Some("revoked") if action == "revoke" => {
                self.pairing
                    .revoke_authenticated(
                        &self.caller_extension_id,
                        install,
                        route.bridge_id(),
                        route.server_instance_id(),
                        route.key_generation(),
                    )
                    .map_err(|_| SessionError::ConnectorUnavailable)?;
                Ok(true)
            }
            _ => Ok(false),
        }
    }

    fn receive_hello(
        &mut self,
        source: Peer,
        message: RpcMessage,
        now_ms: u64,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        if source != Peer::Extension {
            self.state = SessionState::Blocked;
            return Err(SessionError::HelloRequired);
        }
        if now_ms.saturating_sub(self.started_at_ms) >= rpc::HELLO_TIMEOUT_MS {
            self.state = SessionState::Blocked;
            return Err(SessionError::HelloDeadlineExceeded);
        }
        let RpcMessage::Request(request) = message else {
            self.state = SessionState::Blocked;
            return Err(SessionError::HelloRequired);
        };
        if request.method != "bridge.hello" || request.id.is_none() {
            self.state = SessionState::Blocked;
            return Err(SessionError::HelloRequired);
        }
        if hello_extension_id(&request.params)? != self.caller_extension_id {
            self.state = SessionState::Blocked;
            return Err(SessionError::ExtensionIdentityMismatch);
        }
        let install_instance_id = hello_install_instance_id(&request.params)?.to_owned();
        let generation = request
            .params
            .as_object()
            .and_then(|p| p.get("extension"))
            .and_then(Value::as_object)
            .and_then(|p| p.get("load_generation_id"))
            .and_then(Value::as_str)
            .ok_or(SessionError::InvalidState)?
            .to_owned();
        let last_acked_event_sequence = hello_event_cursor(&request.params, &generation)?;
        self.core_codec = Some(ConnectorCodec::with_profile(
            generation.clone(),
            self.transport_profile,
        ));
        #[cfg(not(feature = "local-development"))]
        {
            self.artifact_codec = Some(ArtifactCodec::with_profile(now_ms, self.transport_profile));
        }
        self.event_codec = Some(EventCodec::with_profile(
            generation,
            last_acked_event_sequence,
            self.transport_profile,
        ));
        self.install_instance_id = Some(install_instance_id.clone());
        self.runtime_hello =
            ExtensionRuntimeHello::from_validated_invocation(&self.invocation, &request.params)
                .ok();
        let id = request.id.expect("hello request ID was checked");
        let pairing = self
            .pairing
            .hello(&self.caller_extension_id, &install_instance_id);
        let connection_id = connection_id(self.server_ready)?;
        if self.server_ready {
            self.state = SessionState::Ready;
            return Ok(vec![RoutedMessage {
                target: Peer::Extension,
                payload: hello_success(
                    &id,
                    &connection_id,
                    pairing,
                    NativeHelloAdmission::Inactive,
                    true,
                )
                .encode(),
            }]);
        }
        if pairing.server_state != "paired" || self.runtime_hello.is_none() {
            self.state = SessionState::PairingOnly;
            return Ok(vec![RoutedMessage {
                target: Peer::Extension,
                payload: hello_success(
                    &id,
                    &connection_id,
                    pairing,
                    NativeHelloAdmission::Inactive,
                    false,
                )
                .encode(),
            }]);
        }
        #[cfg(feature = "local-development")]
        {
            // This starts only the separately domain-bound development
            // challenge and hello-only worker. It cannot construct a runtime
            // route, negotiate capabilities, or forward application traffic.
            self.pending_native_hello = Some(PendingNativeHello {
                id,
                connection_id,
                pairing,
            });
            self.core_connect_requested = true;
            self.state = SessionState::Authenticating;
            Ok(Vec::new())
        }
        #[cfg(not(feature = "local-development"))]
        {
            self.pending_native_hello = Some(PendingNativeHello {
                id,
                connection_id,
                pairing,
            });
            self.core_connect_requested = true;
            self.state = SessionState::Authenticating;
            Ok(Vec::new())
        }
    }

    fn receive_pairing_only(
        &mut self,
        source: Peer,
        message: RpcMessage,
        now_ms: u64,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        if source != Peer::Extension {
            return Err(SessionError::InvalidState);
        }
        let RpcMessage::Request(request) = message else {
            return Err(SessionError::UnknownCorrelation);
        };
        if request.method == "bridge.ping" {
            return Ok(self.answer_ping(source, request, now_ms));
        }
        let id = request.id.ok_or(SessionError::InvalidState)?;
        let install_instance_id = self
            .install_instance_id
            .as_deref()
            .ok_or(SessionError::InvalidState)?
            .to_owned();
        let response = match request.method.as_str() {
            "pairing.status" => {
                let mut value = self
                    .pairing
                    .status(&self.caller_extension_id, &install_instance_id);
                if let Some(connection) = &self.core_connection {
                    append_core_status(&mut value, connection.status());
                }
                pairing_response(&id, Ok(value))
            }
            "pairing.exchange" => {
                let result = self.pairing.exchange(
                    &request.params,
                    &self.caller_extension_id,
                    &install_instance_id,
                );
                pairing_response(&id, result)
            }
            "pairing.disconnect" => {
                self.core_connection = None;
                self.core_connect_requested = false;
                pairing_response(
                    &id,
                    self.pairing
                        .disconnect(&self.caller_extension_id, &install_instance_id),
                )
            }
            _ => application_error(
                &id,
                "NOT_PAIRED",
                "Pair Agent Zero before using this native method.",
                "not_applied",
                false,
            ),
        };
        Ok(vec![RoutedMessage {
            target: source,
            payload: response.encode(),
        }])
    }

    fn finish_inactive_native_hello(&mut self) -> Result<Vec<RoutedMessage>, SessionError> {
        let pending = self
            .pending_native_hello
            .take()
            .ok_or(SessionError::InvalidState)?;
        self.admitted_route = None;
        #[cfg(feature = "local-development")]
        {
            self.development_route = None;
        }
        self.state = SessionState::PairingOnly;
        Ok(vec![RoutedMessage {
            target: Peer::Extension,
            payload: hello_success(
                &pending.id,
                &pending.connection_id,
                pending.pairing,
                NativeHelloAdmission::Inactive,
                false,
            )
            .encode(),
        }])
    }

    fn validate_core_command_profile(&self, command: &CoreCommand) -> Result<(), SessionError> {
        if command.transport_profile != self.transport_profile {
            return Err(SessionError::ConnectorPacketInvalid);
        }
        Ok(())
    }

    fn answer_ping(&self, source: Peer, request: RpcRequest, now_ms: u64) -> Vec<RoutedMessage> {
        let Some(id) = request.id else {
            return Vec::new();
        };
        let nonce = request
            .params
            .as_object()
            .and_then(|fields| fields.get("nonce"))
            .cloned()
            .unwrap_or(Value::Null);
        let mut result = BTreeMap::new();
        result.insert("nonce".to_owned(), nonce);
        result.insert(
            "observed_at_ms".to_owned(),
            Value::Number(now_ms.to_string()),
        );
        vec![RoutedMessage {
            target: source,
            payload: RpcMessage::Response(RpcResponse {
                id,
                result: Ok(Value::Object(result)),
            })
            .encode(),
        }]
    }

    fn forward_request(
        &mut self,
        source: Peer,
        request: RpcRequest,
        now_ms: u64,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        let Some(id) = request.id.clone() else {
            return Ok(vec![RoutedMessage {
                target: source.opposite(),
                payload: RpcMessage::Request(request).encode(),
            }]);
        };
        let key = CorrelationKey {
            response_source: source.opposite(),
            id,
        };
        if self.pending.contains_key(&key) || self.completed.contains(&key) {
            return Err(SessionError::DuplicateCorrelation);
        }
        if self.pending.len() >= MAX_PENDING_CORRELATIONS {
            return Err(SessionError::CorrelationCapacityExceeded);
        }
        let timeout_ms = request_timeout_ms(&request)?;
        let deadline_ms = now_ms
            .checked_add(timeout_ms)
            .ok_or(SessionError::ClockInvalid)?;
        self.pending.insert(
            key,
            PendingCorrelation {
                request_source: source,
                deadline_ms,
                effect_possible: request_may_have_effect(&request),
            },
        );
        Ok(vec![RoutedMessage {
            target: source.opposite(),
            payload: RpcMessage::Request(request).encode(),
        }])
    }

    fn forward_browser_event(
        &mut self,
        source: Peer,
        request: RpcRequest,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        if source != Peer::Extension {
            return Err(SessionError::InvalidState);
        }
        let prepared = self
            .event_codec
            .as_mut()
            .ok_or(SessionError::InvalidState)?
            .prepare_event(&request)
            .map_err(map_event_codec_error)?;
        if let Some(packet) = prepared.packet().map(str::to_owned) {
            self.core_connection
                .as_ref()
                .ok_or(SessionError::ConnectorUnavailable)?
                .send_result(packet)
                .map_err(|_| SessionError::ConnectorUnavailable)?;
        }
        self.event_codec
            .as_mut()
            .ok_or(SessionError::InvalidState)?
            .commit_event(prepared)
            .map_err(map_event_codec_error)?;
        // `browser.event` is notification-only. The durable extension WAL is
        // pruned solely by the separately correlated browser.ack_events flow.
        Ok(Vec::new())
    }

    fn defer_upload(&mut self, payload: &[u8]) -> Result<bool, SessionError> {
        let Ok(RpcMessage::Request(request)) = parse_message(payload, Peer::Server) else {
            return Ok(false);
        };
        let Some(params) = request.params.as_object() else {
            return Ok(false);
        };
        if matches!(
            request.method.as_str(),
            "browser.cancel" | "browser.finalize_turn"
        ) {
            self.deferred_uploads.retain(|_, upload| {
                let Ok(RpcMessage::Request(deferred)) =
                    parse_message(&upload.payload, Peer::Server)
                else {
                    return false;
                };
                let Some(binding) = deferred.params.as_object() else {
                    return false;
                };
                let same_turn = ["context_id", "browser_session_id", "turn_id"]
                    .iter()
                    .all(|key| params.get(*key) == binding.get(*key));
                !(same_turn
                    && (request.method == "browser.finalize_turn"
                        || params.get("op_id") == binding.get("op_id")))
            });
            return Ok(false);
        }
        if request.method != "browser.perform"
            || params.get("action").and_then(Value::as_str) != Some("upload_file")
        {
            return Ok(false);
        }
        if !self.transport_profile.permits_context() || self.deferred_uploads.len() >= 16 {
            return Err(SessionError::CorrelationCapacityExceeded);
        }
        let op_id = params
            .get("op_id")
            .and_then(Value::as_str)
            .ok_or(SessionError::ConnectorPacketInvalid)?;
        let artifact_id = params
            .get("args")
            .and_then(Value::as_object)
            .and_then(|args| args.get("artifact_id"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty() && value.len() <= 256)
            .ok_or(SessionError::ConnectorPacketInvalid)?;
        if self.deferred_uploads.contains_key(op_id) {
            return Err(SessionError::DuplicateCorrelation);
        }
        self.deferred_uploads.insert(
            op_id.into(),
            DeferredUpload {
                rpc_id: request.id.ok_or(SessionError::ConnectorPacketInvalid)?,
                artifact_id: artifact_id.into(),
                payload: payload.to_vec(),
            },
        );
        Ok(true)
    }

    fn release_completed_uploads(&mut self) -> Vec<RoutedMessage> {
        let ready = self
            .deferred_uploads
            .iter()
            .filter(|(op_id, upload)| {
                self.artifact_codec
                    .as_ref()
                    .is_some_and(|codec| codec.input_ready(op_id, &upload.artifact_id))
                    && self.pending.contains_key(&CorrelationKey {
                        response_source: Peer::Extension,
                        id: upload.rpc_id.clone(),
                    })
            })
            .map(|(op_id, _)| op_id.clone())
            .collect::<Vec<_>>();
        ready
            .into_iter()
            .filter_map(|op_id| self.deferred_uploads.remove(&op_id))
            .map(|upload| RoutedMessage {
                target: Peer::Extension,
                payload: upload.payload,
            })
            .collect()
    }

    fn forward_output_artifact(
        &mut self,
        request: RpcRequest,
        now_ms: u64,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        let payload = self
            .artifact_codec
            .as_mut()
            .ok_or(SessionError::InvalidState)?
            .request(
                &request,
                self.core_codec.as_ref().ok_or(SessionError::InvalidState)?,
                now_ms,
            )
            .map_err(map_artifact_codec_error)?;
        Ok(vec![RoutedMessage {
            target: Peer::Server,
            payload,
        }])
    }

    fn forward_response(
        &mut self,
        source: Peer,
        response: RpcResponse,
        now_ms: u64,
    ) -> Result<Vec<RoutedMessage>, SessionError> {
        let key = CorrelationKey {
            response_source: source,
            id: response.id.clone(),
        };
        let Some(pending) = self.pending.remove(&key) else {
            // A timed-out or already completed response is a harmless late
            // receipt. It must not terminate unrelated work or resolve twice.
            return if self.completed.contains(&key) {
                Ok(Vec::new())
            } else {
                Err(SessionError::UnknownCorrelation)
            };
        };
        self.mark_completed(key);
        if now_ms >= pending.deadline_ms {
            return Ok(vec![RoutedMessage {
                target: pending.request_source,
                payload: application_error(
                    &response.id,
                    if pending.effect_possible {
                        "OUTCOME_UNKNOWN"
                    } else {
                        "DEADLINE_EXCEEDED"
                    },
                    "The native relay request deadline expired.",
                    if pending.effect_possible {
                        "unknown"
                    } else {
                        "not_applied"
                    },
                    false,
                )
                .encode(),
            }]);
        }
        Ok(vec![RoutedMessage {
            target: pending.request_source,
            payload: RpcMessage::Response(response).encode(),
        }])
    }

    fn observe_clock(&mut self, now_ms: u64) -> Result<(), SessionError> {
        if now_ms < self.last_now_ms {
            self.state = SessionState::Blocked;
            return Err(SessionError::ClockInvalid);
        }
        self.last_now_ms = now_ms;
        Ok(())
    }

    fn mark_completed(&mut self, key: CorrelationKey) {
        if !self.completed.insert(key.clone()) {
            return;
        }
        self.completed_order.push_back(key);
        if self.completed_order.len() > MAX_COMPLETED_CORRELATIONS {
            if let Some(oldest) = self.completed_order.pop_front() {
                self.completed.remove(&oldest);
            }
        }
    }
}

impl Drop for RelaySession {
    fn drop(&mut self) {
        self.core_connection = None;
        self.artifact_codec = None;
        #[cfg(test)]
        if let Some(parent) = self.private_spool_parent.take() {
            let _ = std::fs::remove_dir(parent);
        }
    }
}

fn map_event_codec_error(error: EventCodecError) -> SessionError {
    match error {
        EventCodecError::Capacity => SessionError::CorrelationCapacityExceeded,
        EventCodecError::ClockInvalid => SessionError::ClockInvalid,
        EventCodecError::UnknownResponse => SessionError::UnknownCorrelation,
        EventCodecError::InvalidPacket
        | EventCodecError::InvalidResponse
        | EventCodecError::UnsupportedEvent => SessionError::ConnectorPacketInvalid,
    }
}

fn map_artifact_codec_error(error: ArtifactCodecError) -> SessionError {
    match error {
        ArtifactCodecError::Capacity => SessionError::CorrelationCapacityExceeded,
        ArtifactCodecError::ClockInvalid => SessionError::ClockInvalid,
        ArtifactCodecError::PrivateSpoolUnavailable => SessionError::ConnectorUnavailable,
        ArtifactCodecError::UnknownResponse => SessionError::UnknownCorrelation,
        ArtifactCodecError::InvalidPacket
        | ArtifactCodecError::InvalidResponse
        | ArtifactCodecError::UnsupportedEvent => SessionError::ConnectorPacketInvalid,
    }
}

fn hello_event_cursor(params: &Value, load_generation_id: &str) -> Result<u64, SessionError> {
    let cursors = params
        .as_object()
        .and_then(|fields| fields.get("resume"))
        .and_then(Value::as_object)
        .and_then(|fields| fields.get("event_cursors"))
        .and_then(Value::as_array)
        .ok_or(SessionError::InvalidState)?;
    let mut matched = None;
    for cursor in cursors {
        let fields = cursor.as_object().ok_or(SessionError::InvalidState)?;
        if fields.get("load_generation_id").and_then(Value::as_str) != Some(load_generation_id) {
            continue;
        }
        let sequence = fields
            .get("last_acked_event_sequence")
            .and_then(Value::as_u64)
            .ok_or(SessionError::InvalidState)?;
        if sequence > 9_007_199_254_740_991 {
            return Err(SessionError::InvalidState);
        }
        if matched.replace(sequence).is_some() {
            return Err(SessionError::InvalidState);
        }
    }
    Ok(matched.unwrap_or(0))
}

fn request_may_have_effect(request: &RpcRequest) -> bool {
    match request.method.as_str() {
        "bridge.ping" | "context.list" | "agent.status" | "browser.reconcile" => false,
        "browser.perform" => !matches!(
            request
                .params
                .as_object()
                .and_then(|p| p.get("action"))
                .and_then(Value::as_str),
            Some("status" | "list" | "state" | "content" | "detail" | "screenshot")
        ),
        // Subscriptions, message sends, artifacts, cancellation and finalization
        // can change remote state too. A local timer cannot disprove that effect.
        _ => true,
    }
}

fn append_core_status(value: &mut Value, status: ConnectionStatus) {
    let (code, message) = match status {
        ConnectionStatus::Connecting => ("CONNECTOR_CONNECTING", "Connecting to Agent Zero."),
        ConnectionStatus::AuthenticatedRuntimePending => (
            "CONNECTOR_AUTHENTICATED_RUNTIME_PENDING",
            "The signed connection is established; browser operations are not available yet.",
        ),
        ConnectionStatus::Failed => (
            "CONNECTOR_CONNECTION_FAILED",
            "The signed connection could not be maintained. Reopen the extension to try a fresh connection.",
        ),
        _ => return,
    };
    if let Value::Object(fields) = value {
        if let Some(Value::Array(diagnostics)) = fields.get_mut("diagnostics") {
            diagnostics.push(Value::Object(BTreeMap::from([
                ("code".into(), Value::String(code.into())),
                ("message".into(), Value::String(message.into())),
                ("action".into(), Value::Null),
            ])));
        }
    }
}

#[derive(Clone, Copy)]
enum NativeHelloAdmission<'a> {
    Inactive,
    Production(&'a AdmittedRuntimeRoute),
    #[cfg(feature = "local-development")]
    Development(&'a DevelopmentRuntimeRoute),
}

fn hello_success(
    id: &str,
    connection_id: &str,
    pairing: PairingHello,
    admission: NativeHelloAdmission<'_>,
    fixture_ready: bool,
) -> RpcMessage {
    let admitted = !matches!(admission, NativeHelloAdmission::Inactive);
    let companion_instance_id = match admission {
        NativeHelloAdmission::Inactive => pairing.companion_instance_id.clone(),
        NativeHelloAdmission::Production(route) => route.companion_instance_id().to_owned(),
        #[cfg(feature = "local-development")]
        NativeHelloAdmission::Development(route) => route.companion_instance_id().to_owned(),
    };
    let server_instance_id = match admission {
        NativeHelloAdmission::Inactive => pairing.server_instance_id.clone(),
        NativeHelloAdmission::Production(route) => Some(route.server_instance_id().to_owned()),
        #[cfg(feature = "local-development")]
        NativeHelloAdmission::Development(route) => Some(route.server_instance_id().to_owned()),
    };
    let mut companion = BTreeMap::new();
    companion.insert(
        "instance_id".to_owned(),
        Value::String(if fixture_ready {
            "fixture-instance".to_owned()
        } else {
            companion_instance_id
        }),
    );
    companion.insert(
        "version".to_owned(),
        Value::String(COMPANION_VERSION.to_owned()),
    );
    companion.insert(
        "platform".to_owned(),
        Value::String(runtime_platform().to_owned()),
    );
    companion.insert(
        "arch".to_owned(),
        Value::String(crate::platform::architecture().to_owned()),
    );

    let mut server = BTreeMap::new();
    server.insert(
        "state".to_owned(),
        Value::String(
            (if fixture_ready || admitted {
                "paired"
            } else {
                pairing.server_state
            })
            .to_owned(),
        ),
    );
    server.insert(
        "instance_id".to_owned(),
        if fixture_ready {
            Value::String("fixture-server".to_owned())
        } else {
            server_instance_id.map_or(Value::Null, Value::String)
        },
    );
    server.insert("label".to_owned(), Value::String("Agent Zero".to_owned()));

    let mut limits = BTreeMap::new();
    limits.insert(
        "max_json_frame_bytes".to_owned(),
        Value::Number(rpc::MAX_NATIVE_FRAME_BYTES.to_string()),
    );
    limits.insert(
        "artifact_chunk_bytes".to_owned(),
        Value::Number(rpc::MAX_ARTIFACT_CHUNK_RAW_BYTES.to_string()),
    );
    limits.insert(
        "max_artifact_bytes".to_owned(),
        Value::Number(rpc::MAX_ARTIFACT_BYTES.to_string()),
    );

    let mut result = BTreeMap::new();
    result.insert(
        "protocol".to_owned(),
        Value::String(rpc::BRIDGE_PROTOCOL_VERSIONED.to_owned()),
    );
    result.insert(
        "contract_version".to_owned(),
        Value::Number(rpc::CONTRACT_VERSION.to_string()),
    );
    result.insert(
        "connection_id".to_owned(),
        Value::String(connection_id.to_owned()),
    );
    result.insert("companion".to_owned(), Value::Object(companion));
    result.insert("server".to_owned(), Value::Object(server));
    result.insert("limits".to_owned(), Value::Object(limits));
    result.insert(
        "negotiated".to_owned(),
        match admission {
            NativeHelloAdmission::Inactive => Value::Object(BTreeMap::from([
                ("actions".to_owned(), Value::Array(Vec::new())),
                ("features".to_owned(), Value::Array(Vec::new())),
            ])),
            NativeHelloAdmission::Production(route) => route.native_negotiated(),
            #[cfg(feature = "local-development")]
            NativeHelloAdmission::Development(route) => route.native_negotiated(),
        },
    );
    if let NativeHelloAdmission::Production(route) = admission {
        result.insert("activation".to_owned(), route.native_activation());
    }
    #[cfg(feature = "local-development")]
    match admission {
        NativeHelloAdmission::Development(route) => {
            result.insert("development_admission".to_owned(), route.native_admission());
        }
        NativeHelloAdmission::Inactive => {
            result.insert(
                "development".to_owned(),
                crate::pairing::development_projection(),
            );
        }
        NativeHelloAdmission::Production(_) => {}
    }
    RpcMessage::Response(RpcResponse {
        id: id.to_owned(),
        result: Ok(Value::Object(result)),
    })
}

fn connection_id(fixture: bool) -> Result<String, SessionError> {
    if fixture {
        return Ok("fixture-connection".to_owned());
    }
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| SessionError::EntropyUnavailable)?;
    let mut value = String::with_capacity(43);
    value.push_str("native:");
    for byte in bytes {
        write!(&mut value, "{byte:02x}").map_err(|_| SessionError::EntropyUnavailable)?;
    }
    Ok(value)
}

fn pairing_response(id: &str, result: Result<Value, PairingFailure>) -> RpcMessage {
    RpcMessage::Response(RpcResponse {
        id: id.to_owned(),
        result: result.map_err(|error| RpcErrorObject {
            code: -32_010,
            message: error.message().to_owned(),
            data: Some(pairing_error_data(error)),
        }),
    })
}

fn pairing_error_data(error: PairingFailure) -> Value {
    let mut data = BTreeMap::new();
    data.insert("a0_code".to_owned(), Value::String(error.code().to_owned()));
    data.insert(
        "outcome".to_owned(),
        Value::String(error.outcome().to_owned()),
    );
    data.insert("retryable".to_owned(), Value::Bool(error.retryable()));
    data.insert("details".to_owned(), Value::Object(BTreeMap::new()));
    Value::Object(data)
}

fn application_error(
    id: &str,
    a0_code: &str,
    message: &str,
    outcome: &str,
    retryable: bool,
) -> RpcMessage {
    let mut data = BTreeMap::new();
    data.insert("a0_code".to_owned(), Value::String(a0_code.to_owned()));
    data.insert("outcome".to_owned(), Value::String(outcome.to_owned()));
    data.insert("retryable".to_owned(), Value::Bool(retryable));
    data.insert("details".to_owned(), Value::Object(BTreeMap::new()));
    RpcMessage::Response(RpcResponse {
        id: id.to_owned(),
        result: Err(RpcErrorObject {
            code: -32_010,
            message: message.to_owned(),
            data: Some(Value::Object(data)),
        }),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pairing::CredentialRecord;
    use crate::runtime_handshake::{
        fixture_admitted_ack, parse_core_hello_ack, CoreHelloExpectation, CredentialRuntimeBinding,
    };

    const ORIGIN: &str = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/";

    #[cfg(not(feature = "local-development"))]
    #[test]
    fn upload_dispatch_waits_for_private_input_and_is_withdrawn_by_cancel() {
        let mut session = ready_session();
        let RpcMessage::Request(mut request) = parse_message(
            include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json"),
            Peer::Server,
        )
        .unwrap() else {
            panic!()
        };
        let params = match &mut request.params {
            Value::Object(params) => params,
            _ => panic!(),
        };
        params.insert("action".into(), Value::String("upload_file".into()));
        params.insert(
            "args".into(),
            Value::Object(BTreeMap::from([
                ("ref".into(), Value::String("ref-1".into())),
                (
                    "expected_action_class".into(),
                    Value::String("external_side_effect".into()),
                ),
                ("artifact_id".into(), Value::String("input-1".into())),
                ("mime_type".into(), Value::String("text/plain".into())),
                ("byte_count".into(), Value::Number("3".into())),
                (
                    "sha256".into(),
                    Value::String(format!("sha256:{}", "a".repeat(64))),
                ),
            ])),
        );
        params.insert(
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
        let mut cancel = BTreeMap::from([
            ("contract_version".into(), Value::Number("1".into())),
            ("control_id".into(), Value::String("cancel-1".into())),
            ("reason".into(), Value::String("user_requested".into())),
        ]);
        for key in [
            "context_id",
            "browser_session_id",
            "turn_id",
            "op_id",
            "action_id",
        ] {
            cancel.insert(key.into(), params[key].clone());
        }
        let payload = RpcMessage::Request(request).encode();
        let routed = session.receive(Peer::Server, &payload, 1_002).unwrap();
        assert_eq!(routed.len(), 1);
        assert!(session.defer_upload(&routed[0].payload).unwrap());
        assert_eq!(session.deferred_uploads.len(), 1);
        assert!(session.release_completed_uploads().is_empty());
        let cancel = RpcMessage::Request(RpcRequest {
            id: Some("cancel-1".into()),
            method: "browser.cancel".into(),
            params: Value::Object(cancel),
        })
        .encode();
        assert!(!session.defer_upload(&cancel).unwrap());
        assert!(session.deferred_uploads.is_empty());
    }

    fn hello(extension_id: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"jsonrpc\":\"2.0\",\"id\":\"hello-1\",\"method\":\"bridge.hello\",\"params\":{{",
                "\"protocol\":\"a0.browser-bridge\",\"contract\":{{\"min\":1,\"max\":1}},",
                "\"extension\":{{\"id\":\"{extension_id}\",\"version\":\"0.1.0\",\"manifest_version\":3,",
                "\"install_instance_id\":\"install-1\",\"load_generation_id\":\"generation-1\"}},",
                "\"browser\":{{\"family\":\"chrome\",\"version\":\"146.0.0.0\"}},",
                "\"capabilities\":{{\"actions\":[\"open\"],\"features\":[\"tab_leases_v1\"],\"cdp_domains\":[]}},",
                "\"resume\":{{\"event_cursors\":[],\"inflight_op_ids\":[],\"lease_digest\":\"sha256:0000000000000000000000000000000000000000000000000000000000000000\"}}",
                "}}}}"
            ),
            extension_id = extension_id,
        )
        .into_bytes()
    }

    fn ready_session() -> RelaySession {
        let mut session = RelaySession::fixture(ORIGIN, 1_000);
        let response = session
            .receive(
                Peer::Extension,
                &hello("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                1_001,
            )
            .unwrap();
        assert_eq!(response.len(), 1);
        assert_eq!(response[0].target, Peer::Extension);
        assert_eq!(session.state(), SessionState::Ready);
        session
    }

    #[test]
    fn production_pairing_status_and_disconnect_remain_native_local() {
        let mut session = ready_session();
        let output = session.receive(Peer::Extension, br#"{"jsonrpc":"2.0","id":"status-local","method":"pairing.status","params":{"contract_version":1}}"#, 1002).unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0].target, Peer::Extension);
        assert_eq!(session.pending_count(), 0);
        assert_eq!(session.state(), SessionState::Ready);
        let output = session.receive(Peer::Extension, br#"{"jsonrpc":"2.0","id":"disconnect-local","method":"pairing.disconnect","params":{"contract_version":1}}"#, 1003).unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0].target, Peer::Extension);
        assert_eq!(session.state(), SessionState::Blocked);
        assert!(session.install_instance_id.is_none());
    }

    fn admission_hello() -> Vec<u8> {
        String::from_utf8(hello("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
            .unwrap()
            .replace("install-1", "install-fixture")
            .replace("generation-1", "generation-fixture")
            .into_bytes()
    }

    fn authenticating_session(status: ConnectionStatus, include_route: bool) -> RelaySession {
        let invocation = NativeInvocation::fixture(ORIGIN);
        let mut session = RelaySession::from_validated_invocation(&invocation, 1_000);
        let hello = admission_hello();
        let response = session.receive(Peer::Extension, &hello, 1_001).unwrap();
        assert_eq!(response.len(), 1);
        assert_eq!(session.state(), SessionState::PairingOnly);

        let runtime_hello = session.runtime_hello.clone().unwrap();
        let credential = CredentialRecord::fixture("https://agent.example/a0");
        let binding =
            CredentialRuntimeBinding::from_credential(&credential, &runtime_hello).unwrap();
        let expectation = CoreHelloExpectation::new(runtime_hello, binding);
        let route = include_route.then(|| {
            parse_core_hello_ack(
                &fixture_admitted_ack(&expectation, "sid-fixture"),
                &expectation,
                "sid-fixture",
            )
            .unwrap()
        });
        session.pending_native_hello = Some(PendingNativeHello {
            id: "hello-admitted".into(),
            connection_id: "native:fixture-admitted".into(),
            pairing: PairingHello {
                companion_instance_id: "companion-fixture".into(),
                server_state: "paired",
                server_instance_id: Some("server-fixture".into()),
            },
        });
        session.core_connection = Some(CoreConnection::fixture(status, route));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            let mut random = [0_u8; 16];
            getrandom::fill(&mut random).unwrap();
            let suffix = random
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>();
            let parent = std::env::temp_dir().join(format!("a0-session-spool-{suffix}"));
            std::fs::create_dir(&parent).unwrap();
            std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o700)).unwrap();
            session.private_spool_parent = Some(parent);
        }
        session.state = SessionState::Authenticating;
        session
    }

    fn browser_event() -> Vec<u8> {
        concat!(
            "{\"jsonrpc\":\"2.0\",\"method\":\"browser.event\",\"params\":{",
            "\"contract_version\":1,\"event_id\":\"event-1\",",
            "\"load_generation_id\":\"generation-1\",\"event_sequence\":1,",
            "\"delivery\":\"critical\",\"event_type\":\"lease.changed\",",
            "\"observed_at_ms\":1002,\"context_id\":\"context-1\",",
            "\"browser_session_id\":\"session-1\",\"turn_id\":\"turn-1\",",
            "\"op_id\":null,\"action_id\":null,\"data\":{",
            "\"lease_id_digest\":\"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\",",
            "\"browser_id_digest\":\"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\",",
            "\"state\":\"active\",\"ownership\":\"created\",",
            "\"disposition\":\"ephemeral\",\"change\":\"created\",\"reason_code\":null",
            "}}}"
        )
        .as_bytes()
        .to_vec()
    }

    fn install_pending_screenshot(session: &mut RelaySession) {
        let RpcMessage::Request(mut request) = parse_message(
            include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json"),
            Peer::Server,
        )
        .unwrap() else {
            panic!()
        };
        let Value::Object(fields) = &mut request.params else {
            panic!()
        };
        fields.insert("action".into(), Value::String("screenshot".into()));
        fields.insert("op_id".into(), Value::String("op-1".into()));
        fields.insert("bridge_id".into(), Value::String("bridge-1".into()));
        fields.insert(
            "load_generation_id".into(),
            Value::String("generation-1".into()),
        );
        let packet = format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_op".into()),
                Value::Object(BTreeMap::from([
                    (
                        "handlerId".into(),
                        Value::String(crate::connector_codec::HANDLER_ID.into()),
                    ),
                    ("correlationId".into(), Value::String("op-1".into())),
                    ("data".into(), request.params),
                ])),
            ])
            .encode()
        );
        session
            .core_codec
            .as_mut()
            .unwrap()
            .command(&packet, "bridge-1")
            .unwrap();
    }

    fn artifact_begin() -> Vec<u8> {
        concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":\"artifact-rpc-1\",\"method\":\"artifact.begin\",\"params\":{",
            "\"contract_version\":1,\"context_id\":\"context-1\",",
            "\"browser_session_id\":\"browser-session-1\",\"turn_id\":\"turn-1\",",
            "\"action_id\":\"action-1\",\"op_id\":\"op-1\",\"artifact_id\":\"artifact-1\",",
            "\"direction\":\"output\",\"purpose\":\"screenshot\",\"mime_type\":\"image/png\",",
            "\"byte_count\":0,\"sha256\":\"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}}"
        )
        .as_bytes()
        .to_vec()
    }

    #[test]
    fn fixture_authority_negotiates_only_the_exact_caller_extension() {
        let mut session = RelaySession::fixture(ORIGIN, 1_000);
        assert_eq!(session.caller_origin(), ORIGIN);
        assert_eq!(
            session.receive(
                Peer::Extension,
                &hello("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
                1_001,
            ),
            Err(SessionError::ExtensionIdentityMismatch)
        );
        assert_eq!(session.state(), SessionState::Blocked);
    }

    #[test]
    fn fixture_authority_is_bounded_to_the_hello_deadline() {
        let mut session = RelaySession::fixture(ORIGIN, 1_000);
        assert_eq!(
            session.receive(
                Peer::Extension,
                &hello("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                11_000,
            ),
            Err(SessionError::HelloDeadlineExceeded)
        );
    }

    #[test]
    fn unpaired_session_keeps_onboarding_reachable_and_privileged_relay_closed() {
        let invocation = NativeInvocation::fixture(ORIGIN);
        let mut session = RelaySession::from_validated_invocation(&invocation, 1_000);
        let hello_response = session
            .receive(
                Peer::Extension,
                &hello("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                1_001,
            )
            .unwrap();
        assert_eq!(session.state(), SessionState::PairingOnly);
        assert!(std::str::from_utf8(&hello_response[0].payload)
            .unwrap()
            .contains("\"state\":\"unpaired\""));

        let status = session
            .receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"status-1","method":"pairing.status","params":{"contract_version":1}}"#,
                1_002,
            )
            .unwrap();
        assert!(std::str::from_utf8(&status[0].payload)
            .unwrap()
            .contains("\"state\":\"unpaired\""));

        let exchange = session
            .receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"exchange-1","method":"pairing.exchange","params":{"contract_version":1,"pairing_code":"A0B1-DEADBEEF-0123456789ABCDEFGHJKMNPQRSTVWXYZ","server_base_origin":"https://agent.example.test"}}"#,
                1_003,
            )
            .unwrap();
        assert!(std::str::from_utf8(&exchange[0].payload)
            .unwrap()
            .contains("PAIRING_BACKEND_UNAVAILABLE"));

        let context = session
            .receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"context-1","method":"context.list","params":{"contract_version":1}}"#,
                1_004,
            )
            .unwrap();
        assert!(std::str::from_utf8(&context[0].payload)
            .unwrap()
            .contains("NOT_PAIRED"));
        assert_eq!(session.pending_count(), 0);
    }

    #[cfg(unix)]
    #[test]
    fn exact_core_attestation_promotes_and_exposes_only_the_bound_route() {
        let mut session = authenticating_session(ConnectionStatus::Ready, true);
        assert!(session.artifact_route().is_none());
        assert_eq!(
            session.receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"ping-1","method":"bridge.ping","params":{"nonce":"n"}}"#,
                1_002,
            ),
            Err(SessionError::InvalidState)
        );

        let output = session.poll_core(1_003).unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0].target, Peer::Extension);
        assert_eq!(session.state(), SessionState::Ready);
        let hello = std::str::from_utf8(&output[0].payload).unwrap();
        assert!(hello.contains("\"id\":\"hello-admitted\""));
        assert!(hello.contains("\"activation\""));
        assert!(hello.contains("\"bridge_id\":\"bridge-fixture\""));
        assert!(hello.contains("\"tab_leases_v1\""));
        assert!(hello.contains("\"open\""));
        assert!(!hello.contains("\"screenshots_v1\""));

        let route = session.artifact_route().unwrap();
        assert_eq!(route.caller_origin(), ORIGIN);
        assert_eq!(route.install_instance_id(), "install-fixture");
        assert_eq!(route.load_generation_id(), "generation-fixture");
        assert_eq!(route.server_instance_id(), "server-fixture");
        assert_eq!(route.bridge_id(), "bridge-fixture");
        assert_eq!(route.key_generation(), 1);
        assert_eq!(route.connector_sid(), "sid-fixture");
    }

    #[test]
    fn core_denial_or_admission_deadline_returns_paired_inactive_hello() {
        for (status, now_ms) in [
            (ConnectionStatus::Failed, 1_002),
            (ConnectionStatus::Connecting, 11_000),
        ] {
            let mut session = authenticating_session(status, false);
            let output = if status == ConnectionStatus::Connecting {
                session.expire(now_ms).unwrap()
            } else {
                session.poll_core(now_ms).unwrap()
            };
            assert_eq!(output.len(), 1);
            assert_eq!(output[0].target, Peer::Extension);
            assert_eq!(session.state(), SessionState::PairingOnly);
            assert!(session.artifact_route().is_none());
            let hello = std::str::from_utf8(&output[0].payload).unwrap();
            assert!(hello.contains("\"state\":\"paired\""));
            assert!(hello.contains("\"actions\":[]"));
            assert!(hello.contains("\"features\":[]"));
            assert!(!hello.contains("\"activation\""));
        }
    }

    #[test]
    fn browser_event_never_claims_acceptance_without_a_successful_core_queue() {
        let mut session = ready_session();
        assert_eq!(
            session.receive(Peer::Extension, &browser_event(), 1_002),
            Err(SessionError::ConnectorUnavailable)
        );
        assert_eq!(session.pending_count(), 0);
        assert_eq!(session.state(), SessionState::Ready);
    }

    #[test]
    fn relay_rejects_mismatched_worker_and_command_transport_profiles() {
        let production = BrowserTransportProfile::fixture_production();
        let development = BrowserTransportProfile::fixture_development();

        let mut authenticating = authenticating_session(ConnectionStatus::Connecting, false);
        authenticating.transport_profile = development;
        authenticating.core_connection = Some(CoreConnection::fixture_with_profile(
            ConnectionStatus::Connecting,
            None,
            production,
        ));
        assert_eq!(
            authenticating.poll_core(1_002),
            Err(SessionError::ConnectorPacketInvalid)
        );

        let mut ready = ready_session();
        ready.transport_profile = production;
        let command = CoreCommand {
            packet: "42/ws,[]".into(),
            bridge_id: "bridge-1".into(),
            transport_profile: development,
        };
        assert_eq!(
            ready.validate_core_command_profile(&command),
            Err(SessionError::ConnectorPacketInvalid)
        );
        ready.transport_profile = development;
        assert!(ready.validate_core_command_profile(&command).is_ok());
    }

    #[test]
    fn context_transport_remains_production_only_under_synthetic_development_ready_state() {
        let request = br#"{"jsonrpc":"2.0","id":"context-profile-1","method":"context.list","params":{"contract_version":1}}"#;
        let mut session = ready_session();
        session.transport_profile = BrowserTransportProfile::fixture_development();

        assert_eq!(
            session.receive(Peer::Extension, request, 1_002),
            Err(SessionError::InvalidState)
        );
        assert_eq!(session.send_core(request), Err(SessionError::InvalidState));
        assert_eq!(session.pending_count(), 0);
    }

    #[test]
    fn output_artifact_request_maps_only_for_an_exact_pending_operation() {
        let mut missing = ready_session();
        assert_eq!(
            missing.receive(Peer::Extension, &artifact_begin(), 1_002),
            Err(SessionError::ConnectorPacketInvalid)
        );

        let mut session = ready_session();
        install_pending_screenshot(&mut session);
        let routed = session
            .receive(Peer::Extension, &artifact_begin(), 1_002)
            .unwrap();
        assert_eq!(routed.len(), 1);
        assert_eq!(routed[0].target, Peer::Server);
        let packet = std::str::from_utf8(&routed[0].payload).unwrap();
        assert!(packet.starts_with("42/ws,[\"connector_browser_artifact_chunk\""));
        assert!(packet.contains("\"bridge_id\":\"bridge-1\""));
        assert_eq!(
            session.send_core(&routed[0].payload),
            Err(SessionError::ConnectorUnavailable)
        );
        assert_eq!(session.pending_count(), 0);
    }

    #[test]
    fn full_duplex_requests_with_the_same_id_correlate_by_response_source() {
        let mut session = ready_session();
        let extension_request = br#"{"jsonrpc":"2.0","id":"same-id","method":"context.list","params":{"contract_version":1}}"#;
        let server_request = br#"{"jsonrpc":"2.0","id":"same-id","method":"browser.reconcile","params":{"contract_version":1,"control_id":"control-1","expected_contexts":[],"event_cursors":[],"known_control_ids":[]}}"#;

        assert_eq!(
            session
                .receive(Peer::Extension, extension_request, 1_002)
                .unwrap()[0]
                .target,
            Peer::Server
        );
        assert_eq!(
            session
                .receive(Peer::Server, server_request, 1_003)
                .unwrap()[0]
                .target,
            Peer::Extension
        );
        assert_eq!(session.pending_count(), 2);

        let extension_response =
            br#"{"jsonrpc":"2.0","id":"same-id","result":{"reconciled":true}}"#;
        assert_eq!(
            session
                .receive(Peer::Extension, extension_response, 1_004)
                .unwrap()[0]
                .target,
            Peer::Server
        );

        let response = br#"{"jsonrpc":"2.0","id":"same-id","result":{"contexts":[]}}"#;
        assert_eq!(
            session.receive(Peer::Server, response, 1_005).unwrap()[0].target,
            Peer::Extension
        );
        assert_eq!(session.pending_count(), 0);
    }

    #[test]
    fn duplicate_live_ids_and_unknown_responses_are_rejected() {
        let mut session = ready_session();
        let request = br#"{"jsonrpc":"2.0","id":"context-1","method":"context.list","params":{"contract_version":1}}"#;
        session.receive(Peer::Extension, request, 1_002).unwrap();
        assert_eq!(
            session.receive(Peer::Extension, request, 1_003),
            Err(SessionError::DuplicateCorrelation)
        );
        assert_eq!(
            session.receive(
                Peer::Server,
                br#"{"jsonrpc":"2.0","id":"unknown","result":{}}"#,
                1_004,
            ),
            Err(SessionError::UnknownCorrelation)
        );
    }

    #[test]
    fn expiry_returns_one_typed_terminal_error_and_late_response_fails() {
        let mut session = ready_session();
        let request = br#"{"jsonrpc":"2.0","id":"context-1","method":"context.list","params":{"contract_version":1}}"#;
        session.receive(Peer::Extension, request, 1_002).unwrap();
        let expired = session.expire(121_002).unwrap();
        assert_eq!(expired.len(), 1);
        assert_eq!(expired[0].target, Peer::Extension);
        assert!(std::str::from_utf8(&expired[0].payload)
            .unwrap()
            .contains("DEADLINE_EXCEEDED"));
        assert_eq!(
            session.receive(
                Peer::Server,
                br#"{"jsonrpc":"2.0","id":"context-1","result":{}}"#,
                121_003,
            ),
            Ok(Vec::new())
        );
    }

    #[test]
    fn idle_hello_expires_and_forwarded_mutations_never_claim_not_applied() {
        let mut idle = RelaySession::fixture(ORIGIN, 0);
        assert_eq!(
            idle.expire(rpc::HELLO_TIMEOUT_MS),
            Err(SessionError::HelloDeadlineExceeded)
        );
        let mut session = ready_session();
        let request = include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json");
        session.receive(Peer::Server, request, 1_002).unwrap();
        let expired = session.expire(121_002).unwrap();
        assert_eq!(expired.len(), 1);
        let result = std::str::from_utf8(&expired[0].payload).unwrap();
        assert!(result.contains("OUTCOME_UNKNOWN"));
        assert!(result.contains("\"outcome\":\"unknown\""));
        assert!(!result.contains("not_applied"));
        assert!(session.expire(121_003).unwrap().is_empty());
    }

    #[test]
    fn response_at_deadline_emits_timeout_without_terminating_other_work() {
        let mut session = ready_session();
        let request = include_bytes!("../tests/fixtures/native-rpc-v1/browser-perform.valid.json");
        let RpcMessage::Request(parsed) = parse_message(request, Peer::Server).unwrap() else {
            panic!()
        };
        session.receive(Peer::Server, request, 1_002).unwrap();
        let response = RpcMessage::Response(RpcResponse {
            id: parsed.id.unwrap(),
            result: Ok(Value::Object(BTreeMap::new())),
        })
        .encode();
        let expired = session
            .receive(Peer::Extension, &response, 121_002)
            .unwrap();
        assert_eq!(expired.len(), 1);
        assert_eq!(expired[0].target, Peer::Server);
        assert!(std::str::from_utf8(&expired[0].payload)
            .unwrap()
            .contains("OUTCOME_UNKNOWN"));
        assert!(session
            .receive(Peer::Extension, &response, 121_003)
            .unwrap()
            .is_empty());
        assert_eq!(session.state(), SessionState::Ready);
    }

    #[test]
    fn completed_correlation_tombstones_are_bounded() {
        let mut session = ready_session();
        for index in 0..=MAX_COMPLETED_CORRELATIONS {
            session.mark_completed(CorrelationKey {
                response_source: Peer::Server,
                id: format!("request-{index}"),
            });
        }
        assert_eq!(session.completed.len(), MAX_COMPLETED_CORRELATIONS);
        assert!(!session.completed.iter().any(|key| key.id == "request-0"));
        assert!(session
            .completed
            .iter()
            .any(|key| key.id == format!("request-{MAX_COMPLETED_CORRELATIONS}")));
    }
}

#[cfg(all(test, feature = "local-development"))]
mod development_session_tests {
    use super::*;

    fn authenticating_development_session(status: ConnectionStatus) -> RelaySession {
        let invocation = NativeInvocation::fixture(crate::DEVELOPMENT_EXTENSION_ORIGIN);
        let mut session = RelaySession::from_validated_invocation(&invocation, 1_000);
        session.pending_native_hello = Some(PendingNativeHello {
            id: "hello-dev".into(),
            connection_id: "native:development-fixture".into(),
            pairing: PairingHello {
                companion_instance_id: "companion-dev-fixture".into(),
                server_state: "paired",
                server_instance_id: Some("server-dev-fixture".into()),
            },
        });
        session.core_connection = Some(CoreConnection::fixture(status, None));
        session.state = SessionState::Authenticating;
        session
    }

    fn admitted_development_session() -> RelaySession {
        let mut session = authenticating_development_session(ConnectionStatus::Ready);
        let route = crate::development_session::fixture_runtime_route();
        session.install_instance_id = Some(route.install_instance_id().into());
        session.runtime_hello = Some(crate::development_session::fixture_runtime_hello());
        session.core_codec = Some(ConnectorCodec::with_profile(
            route.load_generation_id().into(),
            BrowserTransportProfile::fixture_development(),
        ));
        session.event_codec = Some(EventCodec::with_profile(
            route.load_generation_id().into(),
            0,
            BrowserTransportProfile::fixture_development(),
        ));
        session.core_connection = Some(CoreConnection::fixture_development(
            ConnectionStatus::Ready,
            Some(route),
        ));
        session
    }

    fn development_reconcile_command() -> String {
        let fixture = crate::json::parse(include_bytes!(
            "../tests/fixtures/browser-reconcile-v1.json"
        ))
        .unwrap();
        let mut data = fixture
            .as_object()
            .unwrap()
            .get("core_control")
            .unwrap()
            .as_object()
            .unwrap()
            .clone();
        data.insert(
            "bridge_id".into(),
            Value::String("bridge-dev-fixture".into()),
        );
        data.insert(
            "load_generation_id".into(),
            Value::String("load-dev-fixture".into()),
        );
        format!(
            "42/ws,{}",
            Value::Array(vec![
                Value::String("connector_browser_control".into()),
                Value::Object(BTreeMap::from([
                    (
                        "handlerId".into(),
                        Value::String(crate::development_session::DEVELOPMENT_HANDLER_ID.into()),
                    ),
                    (
                        "correlationId".into(),
                        Value::String("reconcile-dev-1".into()),
                    ),
                    ("data".into(), Value::Object(data)),
                ])),
            ])
            .encode()
        )
    }

    fn assert_inactive_development_hello(message: &RoutedMessage) {
        assert_eq!(message.target, Peer::Extension);
        let response = crate::json::parse(&message.payload).unwrap();
        let result = response
            .as_object()
            .unwrap()
            .get("result")
            .unwrap()
            .as_object()
            .unwrap();
        assert!(!result.contains_key("activation"));
        let negotiated = result.get("negotiated").unwrap().as_object().unwrap();
        assert!(negotiated
            .get("actions")
            .unwrap()
            .as_array()
            .unwrap()
            .is_empty());
        assert!(negotiated
            .get("features")
            .unwrap()
            .as_array()
            .unwrap()
            .is_empty());
        assert_eq!(
            result.get("development"),
            Some(&crate::pairing::development_projection())
        );
        assert_eq!(
            result
                .get("server")
                .unwrap()
                .as_object()
                .unwrap()
                .get("state")
                .and_then(Value::as_str),
            Some("paired")
        );
    }

    #[test]
    fn development_authenticated_hello_finishes_pairing_only_without_route() {
        let mut session = authenticating_development_session(
            ConnectionStatus::AuthenticatedDevelopmentPairingOnly,
        );
        let output = session.poll_core(1_001).unwrap();
        assert_eq!(output.len(), 1);
        assert_inactive_development_hello(&output[0]);
        assert_eq!(session.state(), SessionState::PairingOnly);
        assert!(session.admitted_route.is_none());
        assert!(session.artifact_route().is_none());
        assert!(session.pending_native_hello.is_none());
        let connection = session.core_connection.as_ref().unwrap();
        assert_eq!(
            connection.status(),
            ConnectionStatus::AuthenticatedDevelopmentPairingOnly
        );
        assert!(connection.send_result("42/ws,[]".into()).is_err());
        assert!(session.poll_core(1_002).unwrap().is_empty());
    }

    #[test]
    fn exact_limited_development_admission_promotes_without_activation_or_artifacts() {
        let mut session = admitted_development_session();
        let output = session.poll_core(1_001).unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(session.state(), SessionState::Ready);
        assert!(session.admitted_route.is_none());
        assert!(session.development_route.is_some());
        assert!(session.artifact_route().is_none());
        assert!(session.artifact_codec.is_none());

        let response = crate::json::parse(&output[0].payload).unwrap();
        let result = response
            .as_object()
            .unwrap()
            .get("result")
            .unwrap()
            .as_object()
            .unwrap();
        assert_eq!(
            result.keys().map(String::as_str).collect::<Vec<_>>(),
            vec![
                "companion",
                "connection_id",
                "contract_version",
                "development_admission",
                "limits",
                "negotiated",
                "protocol",
                "server",
            ]
        );
        assert!(result.contains_key("development_admission"));
        assert!(!result.contains_key("development"));
        assert!(!result.contains_key("activation"));
        assert_eq!(
            result.get("negotiated"),
            Some(
                &session
                    .development_route
                    .as_ref()
                    .unwrap()
                    .native_negotiated()
            )
        );

        let artifact = concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":\"artifact-dev\",\"method\":\"artifact.begin\",\"params\":{",
            "\"contract_version\":1,\"context_id\":\"context-1\",",
            "\"browser_session_id\":\"browser-session-1\",\"turn_id\":\"turn-1\",",
            "\"action_id\":\"action-1\",\"op_id\":\"op-1\",\"artifact_id\":\"artifact-1\",",
            "\"direction\":\"output\",\"purpose\":\"screenshot\",\"mime_type\":\"image/png\",",
            "\"byte_count\":0,\"sha256\":\"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\"}}"
        )
        .as_bytes();
        assert_eq!(
            session.receive(Peer::Extension, artifact, 1_002),
            Err(SessionError::InvalidState)
        );
        let context = br#"{"jsonrpc":"2.0","id":"context-dev","method":"context.list","params":{"contract_version":1}}"#;
        assert_eq!(
            session.receive(Peer::Extension, context, 1_003),
            Err(SessionError::InvalidState)
        );
    }

    #[test]
    fn limited_development_admission_drains_reconcile_after_native_hello_projection() {
        let mut session = admitted_development_session();
        let route = session
            .core_connection
            .as_ref()
            .unwrap()
            .take_development_route()
            .unwrap();
        session.core_connection = Some(CoreConnection::fixture_development_with_commands(
            ConnectionStatus::Ready,
            Some(route),
            vec![(development_reconcile_command(), "bridge-dev-fixture".into())],
        ));

        let output = session.poll_core(1_001).unwrap();
        assert_eq!(output.len(), 2);
        assert_eq!(output[0].target, Peer::Extension);
        let hello = crate::json::parse(&output[0].payload).unwrap();
        assert!(hello
            .as_object()
            .unwrap()
            .get("result")
            .unwrap()
            .as_object()
            .unwrap()
            .contains_key("development_admission"));
        assert_eq!(output[1].target, Peer::Extension);
        let RpcMessage::Request(reconcile) =
            rpc::parse_message(&output[1].payload, Peer::Server).unwrap()
        else {
            panic!("expected native reconcile request")
        };
        assert_eq!(reconcile.method, "browser.reconcile");
        assert_eq!(session.pending_count(), 1);
    }

    #[test]
    fn limited_development_core_loss_is_port_fatal() {
        let mut session = admitted_development_session();
        session.poll_core(1_001).unwrap();
        session
            .core_connection
            .as_ref()
            .unwrap()
            .fixture_set_status(ConnectionStatus::Failed);
        assert_eq!(
            session.poll_core(1_002),
            Err(SessionError::ConnectorUnavailable)
        );
    }

    #[test]
    fn limited_development_pairing_status_is_local_without_masking_core_loss() {
        let mut session = admitted_development_session();
        session.poll_core(1_001).unwrap();

        let output = session
            .receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"status-dev","method":"pairing.status","params":{"contract_version":1}}"#,
                1_002,
            )
            .unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0].target, Peer::Extension);
        let response = crate::json::parse(&output[0].payload).unwrap();
        let response = response.as_object().unwrap();
        assert_eq!(
            response.get("id").and_then(Value::as_str),
            Some("status-dev")
        );
        assert!(response.contains_key("result"));
        assert_eq!(session.state(), SessionState::Ready);
        assert!(session.development_route.is_some());
        assert_eq!(session.pending_count(), 0);
        assert_eq!(
            session.core_connection.as_ref().unwrap().status(),
            ConnectionStatus::Ready
        );

        session
            .core_connection
            .as_ref()
            .unwrap()
            .fixture_set_status(ConnectionStatus::Failed);
        assert_eq!(
            session.poll_core(1_003),
            Err(SessionError::ConnectorUnavailable)
        );
    }

    #[test]
    fn limited_development_disconnect_withdraws_route_before_closing_port() {
        let mut session = admitted_development_session();
        session.poll_core(1_001).unwrap();
        assert_eq!(session.state(), SessionState::Ready);
        assert!(session.development_route.is_some());
        assert!(session.core_connection.is_some());

        let output = session
            .receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"disconnect-dev","method":"pairing.disconnect","params":{"contract_version":1}}"#,
                1_002,
            )
            .unwrap();
        assert_eq!(output.len(), 1);
        assert_eq!(output[0].target, Peer::Extension);
        let response = crate::json::parse(&output[0].payload).unwrap();
        let response = response.as_object().unwrap();
        assert_eq!(
            response.get("id").and_then(Value::as_str),
            Some("disconnect-dev")
        );
        assert_eq!(
            response
                .get("result")
                .and_then(Value::as_object)
                .and_then(|result| result.get("state"))
                .and_then(Value::as_str),
            Some("unpaired")
        );
        assert_eq!(session.state(), SessionState::Blocked);
        assert!(session.core_connection.is_none());
        assert!(session.development_route.is_none());
        assert!(session.runtime_hello.is_none());
        assert!(session.install_instance_id.is_none());
        assert!(session.core_codec.is_none());
        assert!(session.event_codec.is_none());
        assert_eq!(session.pending_count(), 0);
        assert_eq!(
            session.receive(
                Peer::Extension,
                br#"{"jsonrpc":"2.0","id":"status-late","method":"pairing.status","params":{"contract_version":1}}"#,
                1_003,
            ),
            Err(SessionError::InvalidState)
        );
    }

    #[test]
    fn development_falls_back_at_eight_seconds_and_late_worker_cannot_promote() {
        let mut session = authenticating_development_session(ConnectionStatus::Connecting);
        assert!(session.expire(8_999).unwrap().is_empty());
        assert_eq!(session.state(), SessionState::Authenticating);

        let output = session.expire(9_000).unwrap();
        assert_eq!(output.len(), 1);
        assert_inactive_development_hello(&output[0]);
        assert_eq!(session.state(), SessionState::PairingOnly);
        assert!(session.core_connection.is_none());
        assert!(session.pending_native_hello.is_none());

        // Even a synthetic late status cannot re-enter the authentication
        // branch after the exact worker handle was dropped at fallback.
        session.core_connection = Some(CoreConnection::fixture(
            ConnectionStatus::AuthenticatedDevelopmentPairingOnly,
            None,
        ));
        assert!(session.poll_core(9_001).unwrap().is_empty());
        assert_eq!(session.state(), SessionState::PairingOnly);
        assert!(session.admitted_route.is_none());
    }
}
