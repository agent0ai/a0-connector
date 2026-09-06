//! Signed Core connection with exact, attested runtime admission.

use std::collections::BTreeMap;
use std::io::ErrorKind;
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::mpsc::TryRecvError;
use std::sync::{
    atomic::{AtomicBool, AtomicU8, Ordering},
    mpsc::{self, Receiver, SyncSender},
    Arc, Mutex,
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
#[cfg(not(feature = "local-development"))]
use ed25519_dalek::{Signer, SigningKey};
use rustls_platform_verifier::BuilderVerifierExt;
use tungstenite::{client::IntoClientRequest, protocol::WebSocketConfig, Message};
use zeroize::Zeroizing;

use crate::artifact::{ArtifactBinding, ArtifactRoute, CurrentRouteAuthorizer};
#[cfg(feature = "local-development")]
use crate::development_session::{
    DevelopmentHelloExpectation, DevelopmentHelloOutcome, DevelopmentRuntimeRoute,
    DevelopmentSessionError,
};
use crate::json::{self, Value};
use crate::pairing::CredentialRecord;
use crate::pairing::PairingService;
use crate::rpc::{valid_opaque_id, valid_server_base_origin};
#[cfg(not(feature = "local-development"))]
use crate::runtime_handshake::{
    parse_core_hello_ack, CoreHelloExpectation, CredentialRuntimeBinding, CORE_HELLO_ACK_ID,
};
use crate::runtime_handshake::{AdmittedRuntimeRoute, ExtensionRuntimeHello};
use crate::transport_profile::BrowserTransportProfile;
#[cfg(all(test, not(feature = "local-development")))]
use crate::transport_profile::PRODUCTION_HANDLER_PATH;

#[cfg(not(feature = "local-development"))]
#[cfg(test)]
const HANDLER: &str = PRODUCTION_HANDLER_PATH;
#[cfg(feature = "local-development")]
const CORE_HELLO_ACK_ID: u64 = 1;
const MAX_PACKET: usize = 512 * 1024;
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const READ_TICK: Duration = Duration::from_millis(250);
const COMMAND_BACKPRESSURE_LIMIT: Duration = Duration::from_secs(1);
const COMMAND_BACKPRESSURE_TICK: Duration = Duration::from_millis(5);
const MAX_ENGINE_PING_INTERVAL_MS: u64 = 60_000;
const MAX_ENGINE_PING_TIMEOUT_MS: u64 = 120_000;
#[cfg(not(feature = "local-development"))]
const RUNTIME_REFRESH_INTERVAL: Duration = Duration::from_secs(15);
#[cfg(not(feature = "local-development"))]
const RUNTIME_REFRESH_TIMEOUT: Duration = Duration::from_secs(8);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub(crate) enum ConnectionStatus {
    Connecting = 0,
    Unpaired = 1,
    AuthenticatedRuntimePending = 2,
    Ready = 3,
    AuthenticatedDevelopmentPairingOnly = 4,
    Failed = 5,
    Stopped = 6,
}

#[cfg(not(feature = "local-development"))]
type ActiveHelloExpectation = CoreHelloExpectation;
#[cfg(feature = "local-development")]
type ActiveHelloExpectation = DevelopmentHelloExpectation;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ConnectError {
    Credential,
    Network,
    InvalidChallenge,
    InvalidPacket,
    Rejected,
    AuthenticationRejected,
    Expired,
    CommandBackpressure,
    Stopped,
}

impl ConnectError {
    fn reason_code(self) -> &'static str {
        match self {
            Self::Credential => "CORE_CREDENTIAL_UNAVAILABLE",
            Self::Network => "CORE_NETWORK_FAILED",
            Self::InvalidChallenge => "CORE_CHALLENGE_INVALID",
            Self::InvalidPacket => "CORE_PACKET_INVALID",
            Self::Rejected => "CORE_RUNTIME_REJECTED",
            Self::AuthenticationRejected => "CORE_AUTHENTICATION_REJECTED",
            Self::Expired => "CORE_DEADLINE_EXPIRED",
            Self::CommandBackpressure => "CORE_COMMAND_BACKPRESSURE_EXPIRED",
            Self::Stopped => "CORE_WORKER_STOPPED",
        }
    }
}

// Retain exactly one already-read packet while the fixed-capacity owner queue
// drains. No replay, queue growth or unbounded wait; EOF cancellation and live
// heartbeat/renewal deadlines still win over delivery.
fn enqueue_command<T>(
    sender: &SyncSender<T>,
    mut command: T,
    stop: &AtomicBool,
    authority_deadline: Instant,
) -> Result<(), ConnectError> {
    let deadline = authority_deadline.min(Instant::now() + COMMAND_BACKPRESSURE_LIMIT);
    loop {
        stopped(stop)?;
        if Instant::now() >= deadline {
            return Err(ConnectError::CommandBackpressure);
        }
        match sender.try_send(command) {
            Ok(()) => return Ok(()),
            Err(mpsc::TrySendError::Disconnected(_)) => return Err(ConnectError::Stopped),
            Err(mpsc::TrySendError::Full(pending)) => command = pending,
        }
        std::thread::sleep(COMMAND_BACKPRESSURE_TICK.min(deadline.saturating_duration_since(Instant::now())));
    }
}

#[cfg(feature = "local-development")]
fn map_development_session_error(error: DevelopmentSessionError) -> ConnectError {
    match error {
        DevelopmentSessionError::Expired => ConnectError::Expired,
        DevelopmentSessionError::CoreDenied => ConnectError::Rejected,
        DevelopmentSessionError::InvalidChallenge => ConnectError::InvalidChallenge,
        DevelopmentSessionError::InvalidBinding | DevelopmentSessionError::InvalidCorePacket => {
            ConnectError::InvalidPacket
        }
    }
}

pub(crate) struct CoreConnection {
    transport_profile: BrowserTransportProfile,
    stop: Arc<AtomicBool>,
    status: Arc<AtomicU8>,
    admitted_route: Arc<Mutex<Option<AdmittedRuntimeRoute>>>,
    #[cfg(feature = "local-development")]
    development_route: Arc<Mutex<Option<DevelopmentRuntimeRoute>>>,
    commands: Receiver<CoreCommand>,
    results: SyncSender<String>,
    result_deadline: Arc<Mutex<Instant>>,
    native_input_closed: Arc<AtomicBool>,
}

struct WorkerChannels {
    commands: SyncSender<CoreCommand>,
    results: Receiver<String>,
    result_deadline: Arc<Mutex<Instant>>,
}

pub(crate) struct CoreCommand {
    pub(crate) packet: String,
    // Loaded from the caller-bound credential, never a server packet claim.
    pub(crate) bridge_id: String,
    pub(crate) transport_profile: BrowserTransportProfile,
}

impl CoreConnection {
    pub(crate) fn start(
        pairing: Arc<PairingService>,
        extension_hello: ExtensionRuntimeHello,
        native_input_closed: Arc<AtomicBool>,
    ) -> Self {
        let transport_profile = BrowserTransportProfile::compiled();
        let worker_profile = transport_profile;
        let (command_sender, commands) = mpsc::sync_channel(8);
        let (results, result_receiver) = mpsc::sync_channel(8);
        let result_deadline = Arc::new(Mutex::new(Instant::now()));
        let channels = WorkerChannels {
            commands: command_sender,
            results: result_receiver,
            result_deadline: Arc::clone(&result_deadline),
        };
        let stop = Arc::new(AtomicBool::new(false));
        let status = Arc::new(AtomicU8::new(ConnectionStatus::Connecting as u8));
        let admitted_route = Arc::new(Mutex::new(None));
        #[cfg(feature = "local-development")]
        let development_route = Arc::new(Mutex::new(None));
        let worker_stop = Arc::clone(&stop);
        let worker_status = Arc::clone(&status);
        let worker_route = Arc::clone(&admitted_route);
        #[cfg(feature = "local-development")]
        let worker_development_route = Arc::clone(&development_route);
        let spawned = std::thread::Builder::new()
            .name("a0-core-connector".into())
            .spawn(move || {
                let result = run(
                    &pairing,
                    extension_hello,
                    &worker_stop,
                    &worker_status,
                    &worker_route,
                    #[cfg(feature = "local-development")]
                    &worker_development_route,
                    &channels,
                    worker_profile,
                );
                if let Err(error) = result {
                    if !worker_stop.load(Ordering::Acquire) {
                        // Fixed enum only: never print upstream errors, packets,
                        // credentials, URLs, paths or request identifiers.
                        eprintln!("a0-browser-bridge: {}", error.reason_code());
                    }
                    worker_status.store(
                        if worker_stop.load(Ordering::Acquire) {
                            ConnectionStatus::Stopped
                        } else {
                            ConnectionStatus::Failed
                        } as u8,
                        Ordering::Release,
                    );
                }
            });
        if spawned.is_err() {
            status.store(ConnectionStatus::Failed as u8, Ordering::Release);
        }
        // Dropping the thread handle does not block native stdin/stdout or EOF.
        // Cancellation is checked between network phases and on every read tick.
        Self {
            transport_profile,
            stop,
            status,
            admitted_route,
            #[cfg(feature = "local-development")]
            development_route,
            commands,
            results,
            result_deadline,
            native_input_closed,
        }
    }

    pub(crate) fn next_command(&self) -> Option<CoreCommand> {
        self.commands.try_recv().ok()
    }

    pub(crate) fn send_result(&self, mut packet: String) -> Result<(), ()> {
        if packet.len() > MAX_PACKET || self.status() != ConnectionStatus::Ready {
            return Err(());
        }
        // The single owning producer retains one packet while the worker drains
        // the same fixed queue. Admission/EOF always wins over delivery; neither
        // a successful enqueue nor a wait grants fresh runtime authority.
        let deadline = Instant::now() + COMMAND_BACKPRESSURE_LIMIT;
        loop {
            if self.stop.load(Ordering::Acquire)
                || self.native_input_closed.load(Ordering::Acquire)
                || self.status() != ConnectionStatus::Ready
            {
                return Err(());
            }
            let authority_deadline = *self.result_deadline.lock().map_err(|_| ())?;
            let remaining = deadline.min(authority_deadline).saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                eprintln!("a0-browser-bridge: CORE_RESULT_BACKPRESSURE_EXPIRED");
                return Err(());
            }
            match self.results.try_send(packet) {
                Ok(()) => return Ok(()),
                Err(mpsc::TrySendError::Disconnected(_)) => return Err(()),
                Err(mpsc::TrySendError::Full(pending)) => packet = pending,
            }
            std::thread::sleep(COMMAND_BACKPRESSURE_TICK.min(remaining));
        }
    }

    pub(crate) fn status(&self) -> ConnectionStatus {
        match self.status.load(Ordering::Acquire) {
            0 => ConnectionStatus::Connecting,
            1 => ConnectionStatus::Unpaired,
            2 => ConnectionStatus::AuthenticatedRuntimePending,
            3 => ConnectionStatus::Ready,
            4 => ConnectionStatus::AuthenticatedDevelopmentPairingOnly,
            5 => ConnectionStatus::Failed,
            _ => ConnectionStatus::Stopped,
        }
    }

    pub(crate) const fn transport_profile(&self) -> BrowserTransportProfile {
        self.transport_profile
    }

    pub(crate) fn take_admitted_route(&self) -> Option<AdmittedRuntimeRoute> {
        if self.status() != ConnectionStatus::Ready {
            return None;
        }
        self.admitted_route.lock().ok()?.take()
    }

    #[cfg(feature = "local-development")]
    pub(crate) fn take_development_route(&self) -> Option<DevelopmentRuntimeRoute> {
        if self.status() != ConnectionStatus::Ready {
            return None;
        }
        self.development_route.lock().ok()?.take()
    }

    pub(crate) fn artifact_authorizer(&self, route: &ArtifactRoute) -> CurrentRouteAuthorizer {
        let expected = route.clone();
        let stop = Arc::clone(&self.stop);
        let status = Arc::clone(&self.status);
        Arc::new(move |binding: &ArtifactBinding| {
            !stop.load(Ordering::Acquire)
                && status.load(Ordering::Acquire) == ConnectionStatus::Ready as u8
                && binding.route() == &expected
        })
    }

    #[cfg(test)]
    pub(crate) fn fixture(
        status: ConnectionStatus,
        admitted_route: Option<AdmittedRuntimeRoute>,
    ) -> Self {
        Self::fixture_with_profile(status, admitted_route, BrowserTransportProfile::compiled())
    }

    #[cfg(test)]
    pub(crate) fn fixture_with_profile(
        status: ConnectionStatus,
        admitted_route: Option<AdmittedRuntimeRoute>,
        transport_profile: BrowserTransportProfile,
    ) -> Self {
        let (_command_sender, commands) = mpsc::sync_channel(1);
        let (results, _result_receiver) = mpsc::sync_channel(1);
        Self {
            transport_profile,
            stop: Arc::new(AtomicBool::new(false)),
            status: Arc::new(AtomicU8::new(status as u8)),
            admitted_route: Arc::new(Mutex::new(admitted_route)),
            #[cfg(feature = "local-development")]
            development_route: Arc::new(Mutex::new(None)),
            commands,
            results,
            result_deadline: Arc::new(Mutex::new(Instant::now() + Duration::from_secs(60))),
            native_input_closed: Arc::new(AtomicBool::new(false)),
        }
    }

    #[cfg(all(test, feature = "local-development"))]
    pub(crate) fn fixture_development(
        status: ConnectionStatus,
        development_route: Option<DevelopmentRuntimeRoute>,
    ) -> Self {
        Self::fixture_development_with_commands(status, development_route, Vec::new())
    }

    #[cfg(all(test, feature = "local-development"))]
    pub(crate) fn fixture_development_with_commands(
        status: ConnectionStatus,
        development_route: Option<DevelopmentRuntimeRoute>,
        packets: Vec<(String, String)>,
    ) -> Self {
        let (command_sender, commands) = mpsc::sync_channel(8);
        for (packet, bridge_id) in packets {
            command_sender
                .try_send(CoreCommand {
                    packet,
                    bridge_id,
                    transport_profile: BrowserTransportProfile::fixture_development(),
                })
                .unwrap();
        }
        let mut connection = Self::fixture_with_profile(
            status,
            None,
            BrowserTransportProfile::fixture_development(),
        );
        connection.commands = commands;
        connection.development_route = Arc::new(Mutex::new(development_route));
        connection
    }

    #[cfg(test)]
    pub(crate) fn fixture_set_status(&self, status: ConnectionStatus) {
        self.status.store(status as u8, Ordering::Release);
    }
}

impl Drop for CoreConnection {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
    }
}

fn stopped(stop: &AtomicBool) -> Result<(), ConnectError> {
    if stop.load(Ordering::Acquire) {
        Err(ConnectError::Stopped)
    } else {
        Ok(())
    }
}

fn now_ms() -> Result<u64, ConnectError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|v| u64::try_from(v.as_millis()).ok())
        .ok_or(ConnectError::Expired)
}

fn object(fields: &[(&str, Value)]) -> Value {
    Value::Object(
        fields
            .iter()
            .map(|(k, v)| ((*k).into(), v.clone()))
            .collect(),
    )
}

fn text(value: &str) -> Value {
    Value::String(value.into())
}

fn nonce() -> Result<String, ConnectError> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| ConnectError::Credential)?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

fn valid_nonce(value: &str) -> bool {
    value.len() == 43
        && URL_SAFE_NO_PAD
            .decode(value)
            .is_ok_and(|bytes| bytes.len() == 32 && URL_SAFE_NO_PAD.encode(bytes) == value)
}

// All keys are fixed ASCII and all values are strings except integer 1. The
// sorted object encoder therefore produces exactly the trust-v1 JCS bytes.
#[cfg(not(feature = "local-development"))]
fn signed_auth(
    credential: &CredentialRecord,
    client_nonce: &str,
    bytes: &[u8],
    received_at_ms: u64,
    transport_profile: BrowserTransportProfile,
) -> Result<(Zeroizing<String>, u64), ConnectError> {
    if bytes.len() > 8 * 1024 || !valid_nonce(client_nonce) {
        return Err(ConnectError::InvalidChallenge);
    }
    let value = json::parse(bytes).map_err(|_| ConnectError::InvalidChallenge)?;
    let fields = value.as_object().ok_or(ConnectError::InvalidChallenge)?;
    let expected = [
        "challenge_id",
        "expires_at_ms",
        "server_base_url",
        "server_instance_id",
        "server_nonce",
        "trust_version",
    ];
    if fields.len() != expected.len()
        || expected.iter().any(|k| !fields.contains_key(*k))
        || fields.get("trust_version").and_then(Value::as_u64) != Some(1)
        || fields.get("server_instance_id").and_then(Value::as_str)
            != Some(&credential.server_instance_id)
        || fields.get("server_base_url").and_then(Value::as_str)
            != Some(&credential.server_base_origin)
    {
        return Err(ConnectError::InvalidChallenge);
    }
    let challenge_id = fields
        .get("challenge_id")
        .and_then(Value::as_str)
        .filter(|v| valid_opaque_id(v))
        .ok_or(ConnectError::InvalidChallenge)?;
    let server_nonce = fields
        .get("server_nonce")
        .and_then(Value::as_str)
        .filter(|v| valid_nonce(v))
        .ok_or(ConnectError::InvalidChallenge)?;
    let expires = fields
        .get("expires_at_ms")
        .and_then(Value::as_u64)
        .ok_or(ConnectError::InvalidChallenge)?;
    if expires <= received_at_ms || expires.saturating_sub(received_at_ms) > 60_000 {
        return Err(ConnectError::Expired);
    }
    let proof = object(&[
        ("aud", text(&credential.server_instance_id)),
        ("bridge_id", text(&credential.bridge_id)),
        ("challenge_id", text(challenge_id)),
        ("client_nonce", text(client_nonce)),
        ("handler", text(transport_profile.handler_path())),
        ("protocol", text("a0-connector.v1")),
        ("server_base_url", text(&credential.server_base_origin)),
        ("server_nonce", text(server_nonce)),
        ("trust_version", Value::Number("1".into())),
    ]);
    let key = SigningKey::from_bytes(&credential.private_seed);
    let signature = URL_SAFE_NO_PAD.encode(key.sign(proof.encode().as_bytes()).to_bytes());
    let auth = object(&[
        (
            "handlers",
            Value::Array(vec![text(transport_profile.handler_path())]),
        ),
        (
            "principal",
            object(&[
                ("type", text(transport_profile.principal_type())),
                ("proof", proof),
                ("signature", text(&signature)),
            ]),
        ),
    ]);
    Ok((Zeroizing::new(format!("40/ws,{}", auth.encode())), expires))
}

struct Endpoint {
    websocket_url: String,
    origin: String,
    host: String,
    port: u16,
}

impl Endpoint {
    fn parse(base: &str) -> Result<Self, ConnectError> {
        if !valid_server_base_origin(base) {
            return Err(ConnectError::Credential);
        }
        let (scheme, rest) = base.split_once("://").ok_or(ConnectError::Credential)?;
        let authority = rest.split('/').next().ok_or(ConnectError::Credential)?;
        let uri: tungstenite::http::Uri = base.parse().map_err(|_| ConnectError::Credential)?;
        let host = uri
            .host()
            .ok_or(ConnectError::Credential)?
            .trim_matches(['[', ']'])
            .to_owned();
        Ok(Self {
            websocket_url: format!(
                "{}://{rest}/socket.io/?EIO=4&transport=websocket",
                if scheme == "https" { "wss" } else { "ws" }
            ),
            origin: format!("{scheme}://{authority}"),
            host,
            port: uri
                .port_u16()
                .unwrap_or(if scheme == "https" { 443 } else { 80 }),
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Phase {
    EngineOpen,
    Namespace,
    Hello,
    Connected,
}

struct Wire {
    phase: Phase,
    auth: Option<Zeroizing<String>>,
    hello: ActiveHelloExpectation,
    namespace_sid: Option<String>,
    admitted_route: Option<AdmittedRuntimeRoute>,
    #[cfg(feature = "local-development")]
    development_route: Option<DevelopmentRuntimeRoute>,
    #[cfg(feature = "local-development")]
    provisional_reconcile: Option<String>,
    deadline: Instant,
    heartbeat: Duration,
    #[cfg(not(feature = "local-development"))]
    next_refresh: Option<Instant>,
    #[cfg(not(feature = "local-development"))]
    refresh_deadline: Option<Instant>,
    #[cfg(not(feature = "local-development"))]
    refresh_sequence: u64,
}

impl Wire {
    fn new(
        auth: Zeroizing<String>,
        challenge_remaining_ms: u64,
        hello: ActiveHelloExpectation,
    ) -> Self {
        Self {
            phase: Phase::EngineOpen,
            auth: Some(auth),
            hello,
            namespace_sid: None,
            admitted_route: None,
            #[cfg(feature = "local-development")]
            development_route: None,
            #[cfg(feature = "local-development")]
            provisional_reconcile: None,
            deadline: Instant::now()
                + HANDSHAKE_TIMEOUT.min(Duration::from_millis(challenge_remaining_ms)),
            heartbeat: Duration::from_secs(60),
            #[cfg(not(feature = "local-development"))]
            next_refresh: None,
            #[cfg(not(feature = "local-development"))]
            refresh_deadline: None,
            #[cfg(not(feature = "local-development"))]
            refresh_sequence: 0,
        }
    }

    fn receive(&mut self, packet: &str) -> Result<Option<Zeroizing<String>>, ConnectError> {
        if Instant::now() >= self.deadline {
            return Err(ConnectError::Expired);
        }
        if packet.len() > MAX_PACKET {
            return Err(ConnectError::InvalidPacket);
        }
        if packet == "2" && self.phase != Phase::EngineOpen {
            if self.phase == Phase::Connected {
                self.deadline = Instant::now() + self.heartbeat;
            }
            return Ok(Some(Zeroizing::new("3".into())));
        }
        #[cfg(not(feature = "local-development"))]
        if self.phase == Phase::Namespace && packet.starts_with("44/ws,") {
            let value = parse_packet(packet, "44/ws,")?;
            let fields = value.as_object().ok_or(ConnectError::InvalidPacket)?;
            if fields.len() == 1
                && fields.get("message").and_then(Value::as_str)
                    == Some("Connection rejected by server")
            {
                return Err(ConnectError::AuthenticationRejected);
            }
            return Err(ConnectError::Rejected);
        }
        if packet == "1" || packet == "41/ws," || packet.starts_with("44/ws,") {
            return Err(ConnectError::Rejected);
        }
        match self.phase {
            Phase::EngineOpen => {
                let fields = parse_packet(packet, "0")?;
                let fields = fields.as_object().ok_or(ConnectError::InvalidPacket)?;
                let interval = integer(fields, "pingInterval")?;
                let timeout = integer(fields, "pingTimeout")?;
                if !matches!(interval, 1..=MAX_ENGINE_PING_INTERVAL_MS)
                    || !matches!(timeout, 1..=MAX_ENGINE_PING_TIMEOUT_MS)
                    || fields
                        .get("sid")
                        .and_then(Value::as_str)
                        .filter(|s| valid_opaque_id(s))
                        .is_none()
                    || fields.get("upgrades").and_then(Value::as_array).is_none()
                    || integer(fields, "maxPayload")? == 0
                {
                    return Err(ConnectError::InvalidPacket);
                }
                self.heartbeat = Duration::from_millis(interval + timeout);
                self.phase = Phase::Namespace;
                Ok(self.auth.take())
            }
            Phase::Namespace => {
                let value = parse_packet(packet, "40/ws,")?;
                let sid = value
                    .as_object()
                    .and_then(|f| f.get("sid"))
                    .and_then(Value::as_str)
                    .filter(|s| valid_opaque_id(s))
                    .ok_or(ConnectError::InvalidPacket)?;
                self.namespace_sid = Some(sid.to_owned());
                self.phase = Phase::Hello;
                Ok(Some(Zeroizing::new(self.hello.packet())))
            }
            Phase::Hello => {
                #[cfg(feature = "local-development")]
                if packet.starts_with("42/ws,") {
                    if self.provisional_reconcile.is_some() {
                        return Err(ConnectError::InvalidPacket);
                    }
                    self.hello
                        .validate_provisional_reconcile(packet)
                        .map_err(map_development_session_error)?;
                    self.provisional_reconcile = Some(packet.to_owned());
                    return Ok(None);
                }
                let prefix = format!("43/ws,{CORE_HELLO_ACK_ID}");
                let value = parse_packet(packet, &prefix)?;
                let sid = self
                    .namespace_sid
                    .as_deref()
                    .ok_or(ConnectError::InvalidPacket)?;
                #[cfg(not(feature = "local-development"))]
                {
                    self.admitted_route = Some(
                        parse_core_hello_ack(&value, &self.hello, sid)
                            .map_err(|_| ConnectError::Rejected)?,
                    );
                }
                #[cfg(feature = "local-development")]
                {
                    match self
                        .hello
                        .parse_ack(&value, sid)
                        .map_err(map_development_session_error)?
                    {
                        DevelopmentHelloOutcome::PairingOnly(_) => {
                            if self.provisional_reconcile.is_some() {
                                return Err(ConnectError::InvalidPacket);
                            }
                        }
                        DevelopmentHelloOutcome::LimitedRuntime(route) => {
                            self.development_route = Some(route);
                        }
                    }
                }
                self.phase = Phase::Connected;
                self.deadline = Instant::now() + self.heartbeat;
                #[cfg(not(feature = "local-development"))]
                {
                    self.next_refresh = Some(Instant::now() + RUNTIME_REFRESH_INTERVAL);
                }
                Ok(None)
            }
            Phase::Connected => Err(ConnectError::InvalidPacket), // no browser/event handlers yet
        }
    }

    #[cfg(not(feature = "local-development"))]
    fn runtime_refresh(&mut self, now: Instant) -> Result<Option<String>, ConnectError> {
        if self
            .refresh_deadline
            .is_some_and(|deadline| now >= deadline)
        {
            return Err(ConnectError::Expired);
        }
        if self.phase != Phase::Connected
            || self.refresh_deadline.is_some()
            || !self.next_refresh.is_some_and(|next| now >= next)
        {
            return Ok(None);
        }
        self.refresh_sequence = self
            .refresh_sequence
            .checked_add(1)
            .ok_or(ConnectError::Expired)?;
        self.hello.renew_correlation(self.refresh_sequence);
        self.refresh_deadline = Some(now + RUNTIME_REFRESH_TIMEOUT);
        self.next_refresh = None;
        Ok(Some(self.hello.packet()))
    }

    #[cfg(not(feature = "local-development"))]
    fn accept_runtime_refresh(&mut self, packet: &str) -> Result<(), ConnectError> {
        let now = Instant::now();
        if !self.refresh_deadline.is_some_and(|deadline| now < deadline) {
            return Err(ConnectError::Expired);
        }
        let value = parse_packet(packet, &format!("43/ws,{CORE_HELLO_ACK_ID}"))?;
        let route = parse_core_hello_ack(
            &value,
            &self.hello,
            self.namespace_sid
                .as_deref()
                .ok_or(ConnectError::InvalidPacket)?,
        )
        .map_err(|_| ConnectError::Rejected)?;
        // A refresh may only preserve the exact admitted connection. Capability,
        // selection or identity changes need a new native handshake, not promotion.
        if self.admitted_route.as_ref() != Some(&route) {
            return Err(ConnectError::Rejected);
        }
        self.refresh_deadline = None;
        self.next_refresh = Some(now + RUNTIME_REFRESH_INTERVAL);
        Ok(())
    }

    #[cfg(feature = "local-development")]
    fn take_provisional_reconcile(&mut self) -> Option<String> {
        self.provisional_reconcile.take()
    }
}

fn parse_packet(packet: &str, prefix: &str) -> Result<Value, ConnectError> {
    json::parse(
        packet
            .strip_prefix(prefix)
            .ok_or(ConnectError::InvalidPacket)?
            .as_bytes(),
    )
    .map_err(|_| ConnectError::InvalidPacket)
}

fn integer(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, ConnectError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(ConnectError::InvalidPacket)
}

fn run(
    pairing: &PairingService,
    extension_hello: ExtensionRuntimeHello,
    stop: &AtomicBool,
    status: &AtomicU8,
    admitted_route: &Mutex<Option<AdmittedRuntimeRoute>>,
    #[cfg(feature = "local-development")] development_route: &Mutex<
        Option<DevelopmentRuntimeRoute>,
    >,
    channels: &WorkerChannels,
    transport_profile: BrowserTransportProfile,
) -> Result<(), ConnectError> {
    if transport_profile != BrowserTransportProfile::compiled() {
        return Err(ConnectError::Credential);
    }
    let Some(credential) = pairing
        .connector_credential(
            extension_hello.extension_id(),
            extension_hello.install_instance_id(),
        )
        .map_err(|_| ConnectError::Credential)?
    else {
        status.store(ConnectionStatus::Unpaired as u8, Ordering::Release);
        return Ok(());
    };
    #[cfg(not(feature = "local-development"))]
    drop(credential); // never retain the old seed during a pending-key socket
    #[cfg(not(feature = "local-development"))]
    if let Some(pending) = pairing
        .pending_rotation(
            extension_hello.extension_id(),
            extension_hello.install_instance_id(),
        )
        .map_err(|_| ConnectError::Credential)?
    {
        let result = run_credential(
            pairing,
            extension_hello.clone(),
            pending.credential,
            Some(&pending.rotation_id),
            stop,
            status,
            admitted_route,
            channels,
            transport_profile,
        );
        // A rejected uncommitted candidate may coexist with the still-valid
        // active key (request loss/expiry). Try that exact retained key once;
        // never delete either key or fall back after network/unknown failure.
        if result != Err(ConnectError::AuthenticationRejected)
            || status.load(Ordering::Acquire) == ConnectionStatus::Ready as u8
        {
            return result;
        }
        stopped(stop)?;
        status.store(ConnectionStatus::Connecting as u8, Ordering::Release);
    }
    #[cfg(not(feature = "local-development"))]
    let credential = pairing
        .connector_credential(
            extension_hello.extension_id(),
            extension_hello.install_instance_id(),
        )
        .map_err(|_| ConnectError::Credential)?
        .ok_or(ConnectError::Credential)?;
    run_credential(
        pairing,
        extension_hello,
        credential,
        #[cfg(not(feature = "local-development"))]
        None,
        stop,
        status,
        admitted_route,
        #[cfg(feature = "local-development")]
        development_route,
        channels,
        transport_profile,
    )
}

fn run_credential(
    pairing: &PairingService,
    extension_hello: ExtensionRuntimeHello,
    credential: CredentialRecord,
    #[cfg(not(feature = "local-development"))] rotation_id: Option<&str>,
    stop: &AtomicBool,
    status: &AtomicU8,
    admitted_route: &Mutex<Option<AdmittedRuntimeRoute>>,
    #[cfg(feature = "local-development")] development_route: &Mutex<
        Option<DevelopmentRuntimeRoute>,
    >,
    channels: &WorkerChannels,
    transport_profile: BrowserTransportProfile,
) -> Result<(), ConnectError> {
    stopped(stop)?;
    let endpoint = Endpoint::parse(&credential.server_base_origin)?;
    #[cfg(not(feature = "local-development"))]
    let rotation_identity = (
        extension_hello.extension_id().to_owned(),
        extension_hello.install_instance_id().to_owned(),
        credential.key_generation(),
    );
    #[cfg(not(feature = "local-development"))]
    let binding = CredentialRuntimeBinding::from_credential(&credential, &extension_hello)
        .map_err(|_| ConnectError::Credential)?;
    #[cfg(not(feature = "local-development"))]
    let hello = CoreHelloExpectation::new(extension_hello, binding);
    #[cfg(feature = "local-development")]
    let hello = DevelopmentHelloExpectation::from_credential(&extension_hello, &credential)
        .map_err(|_| ConnectError::Credential)?;
    let client_nonce = nonce()?;
    #[cfg(not(feature = "local-development"))]
    let request = object(&[
        ("trust_version", Value::Number("1".into())),
        ("bridge_id", text(&credential.bridge_id)),
        ("client_nonce", text(&client_nonce)),
    ]);
    #[cfg(feature = "local-development")]
    let request = hello.challenge_request(&client_nonce);
    let challenge = pairing
        .connector_challenge(&credential.server_base_origin, request.encode().as_bytes())
        .map_err(|_| ConnectError::Network)?;
    #[cfg(not(feature = "local-development"))]
    let (auth, expires) = signed_auth(
        &credential,
        &client_nonce,
        &challenge,
        now_ms()?,
        transport_profile,
    )?;
    #[cfg(feature = "local-development")]
    let (auth, expires) = hello
        .signed_auth(&credential, &client_nonce, &challenge, now_ms()?)
        .map_err(map_development_session_error)?;
    drop(credential); // zeroize the loaded seed before establishing the socket
    #[cfg(not(feature = "local-development"))]
    let commit_rotation = |route: &AdmittedRuntimeRoute| {
        if let Some(rotation_id) = rotation_id {
            if route.key_generation() != rotation_identity.2
                || route.install_instance_id() != rotation_identity.1
            {
                return Err(ConnectError::Credential);
            }
            pairing
                .commit_rotation_authenticated(
                    &rotation_identity.0,
                    &rotation_identity.1,
                    rotation_id,
                    route.key_generation(),
                )
                .map_err(|_| ConnectError::Credential)?;
        }
        Ok(())
    };
    serve(
        endpoint,
        auth,
        expires,
        stop,
        status,
        admitted_route,
        #[cfg(feature = "local-development")]
        development_route,
        Some(channels),
        hello,
        transport_profile,
        #[cfg(not(feature = "local-development"))]
        Some(&commit_rotation),
    )
}

fn serve(
    endpoint: Endpoint,
    auth: Zeroizing<String>,
    expires: u64,
    stop: &AtomicBool,
    status: &AtomicU8,
    admitted_route: &Mutex<Option<AdmittedRuntimeRoute>>,
    #[cfg(feature = "local-development")] development_route: &Mutex<
        Option<DevelopmentRuntimeRoute>,
    >,
    channels: Option<&WorkerChannels>,
    hello: ActiveHelloExpectation,
    transport_profile: BrowserTransportProfile,
    #[cfg(not(feature = "local-development"))] on_admitted: Option<
        &dyn Fn(&AdmittedRuntimeRoute) -> Result<(), ConnectError>,
    >,
) -> Result<(), ConnectError> {
    stopped(stop)?;
    let connect_deadline = Instant::now() + HANDSHAKE_TIMEOUT;
    let addresses = (endpoint.host.as_str(), endpoint.port)
        .to_socket_addrs()
        .map_err(|_| ConnectError::Network)?;
    let mut stream = None;
    for address in addresses.take(4) {
        stopped(stop)?;
        let remaining = connect_deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        if let Ok(socket) =
            TcpStream::connect_timeout(&address, remaining.min(Duration::from_secs(3)))
        {
            stream = Some(socket);
            break;
        }
    }
    let stream = stream.ok_or(ConnectError::Network)?;
    stream
        .set_read_timeout(Some(HANDSHAKE_TIMEOUT))
        .map_err(|_| ConnectError::Network)?;
    stream
        .set_write_timeout(Some(HANDSHAKE_TIMEOUT))
        .map_err(|_| ConnectError::Network)?;
    let mut request = endpoint
        .websocket_url
        .into_client_request()
        .map_err(|_| ConnectError::Credential)?;
    request.headers_mut().insert(
        "Origin",
        endpoint
            .origin
            .parse()
            .map_err(|_| ConnectError::Credential)?,
    );
    request.headers_mut().insert(
        "Referer",
        format!("{}/", endpoint.origin)
            .parse()
            .map_err(|_| ConnectError::Credential)?,
    );
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .map_err(|_| ConnectError::Network)?
    .with_platform_verifier()
    .map_err(|_| ConnectError::Network)?
    .with_no_client_auth();
    let websocket_config = WebSocketConfig::default()
        .read_buffer_size(4096)
        .write_buffer_size(0)
        .max_write_buffer_size(MAX_PACKET * 2)
        .max_message_size(Some(MAX_PACKET))
        .max_frame_size(Some(MAX_PACKET));
    // client_tls_with_config performs one handshake on this exact stream. It
    // does not follow redirects, discover proxies, or add cookies/API headers.
    let (mut socket, _) = tungstenite::client_tls_with_config(
        request,
        stream,
        Some(websocket_config),
        Some(tungstenite::Connector::Rustls(Arc::new(config))),
    )
    .map_err(|_| ConnectError::Network)?;
    let tcp = match socket.get_ref() {
        tungstenite::stream::MaybeTlsStream::Plain(tcp) => tcp,
        tungstenite::stream::MaybeTlsStream::Rustls(tls) => &tls.sock,
        _ => return Err(ConnectError::Network),
    };
    tcp.set_read_timeout(Some(READ_TICK))
        .map_err(|_| ConnectError::Network)?;
    tcp.set_write_timeout(Some(Duration::from_secs(3)))
        .map_err(|_| ConnectError::Network)?;
    let remaining = expires
        .checked_sub(now_ms()?)
        .filter(|v| *v > 0)
        .ok_or(ConnectError::Expired)?;
    let mut wire = Wire::new(auth, remaining, hello);
    loop {
        if stopped(stop).is_err() {
            let _ = socket.send(Message::Text("41/ws,".into()));
            let _ = socket.close(None);
            return Err(ConnectError::Stopped);
        }
        if Instant::now() >= wire.deadline {
            return Err(ConnectError::Expired);
        }
        #[cfg(not(feature = "local-development"))]
        if let Some(packet) = wire.runtime_refresh(Instant::now())? {
            socket
                .send(Message::Text(packet.into()))
                .map_err(|_| ConnectError::Network)?;
        }
        publish_result_deadline(channels, &wire)?;
        if wire.phase == Phase::Connected {
            if status.load(Ordering::Acquire) == ConnectionStatus::Ready as u8 {
                let channels = channels.ok_or(ConnectError::InvalidPacket)?;
                for _ in 0..8 {
                    match channels.results.try_recv() {
                        Ok(packet) => socket
                            .send(Message::Text(packet.into()))
                            .map_err(|_| ConnectError::Network)?,
                        Err(TryRecvError::Empty) => break,
                        Err(TryRecvError::Disconnected) => return Err(ConnectError::Stopped),
                    }
                }
            }
        }
        match socket.read() {
            Ok(Message::Text(packet)) => {
                #[cfg(not(feature = "local-development"))]
                if wire.phase == Phase::Connected && packet.starts_with("43/ws,1[") {
                    wire.accept_runtime_refresh(&packet)?;
                    continue;
                }
                if wire.phase == Phase::Connected
                    && (packet.starts_with("42/ws,") || packet.starts_with("43/ws,"))
                {
                    // The owner validates the browser-only event codec and then
                    // applies its profile-specific admission gate before forwarding.
                    let channels = channels.ok_or(ConnectError::InvalidPacket)?;
                    #[cfg(not(feature = "local-development"))]
                    let bridge_id = wire
                        .admitted_route
                        .as_ref()
                        .ok_or(ConnectError::InvalidPacket)?
                        .bridge_id();
                    #[cfg(feature = "local-development")]
                    let bridge_id = wire
                        .development_route
                        .as_ref()
                        .ok_or(ConnectError::InvalidPacket)?
                        .bridge_id();
                    let command_deadline = wire.deadline;
                    #[cfg(not(feature = "local-development"))]
                    let command_deadline = wire.refresh_deadline
                        .map_or(command_deadline, |deadline| command_deadline.min(deadline));
                    enqueue_command(
                        &channels.commands,
                        CoreCommand {
                            packet: packet.to_string(),
                            bridge_id: bridge_id.to_owned(),
                            transport_profile,
                        },
                        stop,
                        command_deadline,
                    )?;
                    continue;
                }
                if let Some(response) = wire.receive(&packet)? {
                    stopped(stop)?;
                    socket
                        .send(Message::Text(response.as_str().into()))
                        .map_err(|_| ConnectError::Network)?;
                }
                if wire.phase == Phase::Hello {
                    status.store(
                        ConnectionStatus::AuthenticatedRuntimePending as u8,
                        Ordering::Release,
                    );
                }
                if wire.phase == Phase::Connected {
                    publish_result_deadline(channels, &wire)?;
                    #[cfg(not(feature = "local-development"))]
                    if status.load(Ordering::Acquire) != ConnectionStatus::Ready as u8 {
                        let route = wire
                            .admitted_route
                            .clone()
                            .ok_or(ConnectError::InvalidPacket)?;
                        if let Some(commit) = on_admitted {
                            commit(&route)?;
                        }
                        *admitted_route
                            .lock()
                            .map_err(|_| ConnectError::InvalidPacket)? = Some(route);
                        status.store(ConnectionStatus::Ready as u8, Ordering::Release);
                    }
                    #[cfg(feature = "local-development")]
                    if let Some(route) = wire.development_route.clone() {
                        let bridge_id = route.bridge_id().to_owned();
                        *development_route
                            .lock()
                            .map_err(|_| ConnectError::InvalidPacket)? = Some(route);
                        if let Some(packet) = wire.take_provisional_reconcile() {
                            channels
                                .ok_or(ConnectError::InvalidPacket)?
                                .commands
                                .try_send(CoreCommand {
                                    packet,
                                    bridge_id,
                                    transport_profile,
                                })
                                .map_err(|_| ConnectError::InvalidPacket)?;
                        }
                        status.store(ConnectionStatus::Ready as u8, Ordering::Release);
                    } else {
                        status.store(
                            ConnectionStatus::AuthenticatedDevelopmentPairingOnly as u8,
                            Ordering::Release,
                        );
                    }
                }
            }
            Ok(Message::Ping(_)) | Ok(Message::Pong(_)) => {
                socket.flush().map_err(|_| ConnectError::Network)?;
            }
            Err(tungstenite::Error::Io(e))
                if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) =>
            {
                continue
            }
            _ => return Err(ConnectError::InvalidPacket),
        }
    }
}

fn publish_result_deadline(channels: Option<&WorkerChannels>, wire: &Wire) -> Result<(), ConnectError> {
    if let Some(channels) = channels {
        let deadline = wire.deadline;
        #[cfg(not(feature = "local-development"))]
        let deadline = wire.refresh_deadline.map_or(deadline, |refresh| deadline.min(refresh));
        *channels.result_deadline.lock().map_err(|_| ConnectError::InvalidPacket)? = deadline;
    }
    Ok(())
}

#[cfg(all(test, not(feature = "local-development")))]
mod tests {
    use super::*;
    use crate::runtime_handshake::{fixture_admitted_ack, fixture_expectation};
    use std::net::TcpListener;

    const INACTIVE_ACK: &str = "43/ws,1[{\"correlationId\":\"bridge-hello\",\"results\":[{\"handlerId\":\"ws_connector.WsConnector\",\"ok\":true,\"data\":{\"protocol\":\"a0-connector.v1\",\"principal_type\":\"browser_bridge\",\"features\":[],\"connector_session_ready\":false,\"browser_control_ready\":false}}]}]";

    #[test]
    fn result_backpressure_preserves_reconcile_and_critical_event_burst() {
        let mut connection = CoreConnection::fixture(ConnectionStatus::Ready, None);
        let (sender, receiver) = mpsc::sync_channel(8);
        connection.results = sender;
        let packets: Vec<String> = (0..24).map(|index| format!(
            "42/ws,[\"{}\",{{\"fixture_sequence\":{index}}}]",
            if index == 0 { "browser_bridge_control_result" } else { "browser_bridge_event" },
        )).collect();
        for packet in &packets[..8] { connection.send_result(packet.clone()).unwrap(); }
        let remaining = packets[8..].to_vec();
        let (finished, completion) = mpsc::channel();
        std::thread::scope(|scope| {
            scope.spawn(move || {
                let result = remaining.into_iter().try_for_each(|packet| connection.send_result(packet));
                finished.send(result).unwrap();
            });
            // The worker deliberately has not drained any of its eight slots.
            // The ninth valid packet must wait, not terminate the native port.
            assert_eq!(completion.recv_timeout(Duration::from_millis(30)), Err(mpsc::RecvTimeoutError::Timeout));
            for packet in packets {
                assert_eq!(receiver.recv_timeout(Duration::from_secs(2)).unwrap(), packet);
            }
            assert_eq!(completion.recv_timeout(Duration::from_secs(2)).unwrap(), Ok(()));
        });
    }

    #[test]
    fn result_backpressure_cancels_on_eof_stop_status_or_authority_expiry() {
        for cancellation in 0..4 {
            let mut connection = CoreConnection::fixture(ConnectionStatus::Ready, None);
            let (sender, receiver) = mpsc::sync_channel(1);
            connection.results = sender;
            connection.send_result("first".into()).unwrap();
            let stop = Arc::clone(&connection.stop);
            let input_closed = Arc::clone(&connection.native_input_closed);
            let status = Arc::clone(&connection.status);
            let deadline = Arc::clone(&connection.result_deadline);
            let (finished, completion) = mpsc::channel();
            std::thread::scope(|scope| {
                scope.spawn(move || finished.send(connection.send_result("second".into())).unwrap());
                assert_eq!(completion.recv_timeout(Duration::from_millis(15)), Err(mpsc::RecvTimeoutError::Timeout));
                match cancellation {
                    0 => input_closed.store(true, Ordering::Release),
                    1 => stop.store(true, Ordering::Release),
                    2 => status.store(ConnectionStatus::Failed as u8, Ordering::Release),
                    _ => *deadline.lock().unwrap() = Instant::now(),
                }
                assert_eq!(completion.recv_timeout(Duration::from_millis(200)).unwrap(), Err(()));
            });
            assert_eq!(receiver.recv().unwrap(), "first");
            assert!(receiver.try_recv().is_err());
        }
        let mut connection = CoreConnection::fixture(ConnectionStatus::Ready, None);
        let (sender, receiver) = mpsc::sync_channel(1);
        connection.results = sender;
        assert!(connection.send_result("x".repeat(MAX_PACKET + 1)).is_err());
        assert!(receiver.try_recv().is_err());
        drop(receiver);
        assert!(connection.send_result("disconnected".into()).is_err());
    }

    #[test]
    fn command_backpressure_preserves_burst_fifo_and_bounded_queue() {
        let (sender, receiver) = mpsc::sync_channel(8);
        for value in 0..8 { sender.try_send(value).unwrap(); }
        assert!(matches!(sender.try_send(8), Err(mpsc::TrySendError::Full(8))));
        let stop = AtomicBool::new(false);
        std::thread::scope(|scope| {
            let producer = scope.spawn(|| {
                for value in 8..32 {
                    enqueue_command(&sender, value, &stop, Instant::now() + Duration::from_secs(5)).unwrap();
                }
            });
            for expected in 0..32 {
                assert_eq!(receiver.recv_timeout(Duration::from_secs(2)).unwrap(), expected);
            }
            producer.join().unwrap();
        });
        assert!(matches!(receiver.try_recv(), Err(mpsc::TryRecvError::Empty)));
    }

    #[test]
    fn command_backpressure_never_crosses_stop_or_authority_deadline() {
        let (sender, receiver) = mpsc::sync_channel(1);
        sender.try_send(1).unwrap();
        let stopped = AtomicBool::new(true);
        assert_eq!(enqueue_command(&sender, 2, &stopped, Instant::now() + Duration::from_secs(1)), Err(ConnectError::Stopped));
        let running = AtomicBool::new(false);
        assert_eq!(enqueue_command(&sender, 2, &running, Instant::now()), Err(ConnectError::CommandBackpressure));
        assert_eq!(receiver.try_recv().unwrap(), 1);
        assert!(matches!(receiver.try_recv(), Err(mpsc::TryRecvError::Empty)));
        drop(receiver);
        assert_eq!(enqueue_command(&sender, 2, &running, Instant::now() + Duration::from_secs(1)), Err(ConnectError::Stopped));
        assert_eq!(ConnectError::CommandBackpressure.reason_code(), "CORE_COMMAND_BACKPRESSURE_EXPIRED");
    }

    fn challenge(base: &str, expires: u64) -> Vec<u8> {
        object(&[
            ("challenge_id", text("challenge-fixture")),
            ("expires_at_ms", Value::Number(expires.to_string())),
            ("server_base_url", text(base)),
            ("server_instance_id", text("server-fixture")),
            ("server_nonce", text(&URL_SAFE_NO_PAD.encode([9; 32]))),
            ("trust_version", Value::Number("1".into())),
        ])
        .encode()
        .into_bytes()
    }

    fn verify_auth(packet: &str) {
        let auth = parse_packet(packet, "40/ws,").unwrap();
        let fields = auth.as_object().unwrap();
        assert_eq!(fields.len(), 2);
        assert_eq!(
            fields.get("handlers"),
            Some(&Value::Array(vec![text(HANDLER)]))
        );
        let principal = fields["principal"].as_object().unwrap();
        let signature = URL_SAFE_NO_PAD
            .decode(principal["signature"].as_str().unwrap())
            .unwrap();
        let signature = ed25519_dalek::Signature::from_slice(&signature).unwrap();
        SigningKey::from_bytes(&[7; 32])
            .verifying_key()
            .verify_strict(principal["proof"].encode().as_bytes(), &signature)
            .unwrap();
        assert_eq!(
            principal["proof"].as_object().unwrap()["handler"],
            text(HANDLER)
        );
    }

    #[test]
    fn proof_is_signed_and_rejects_mismatch_expiry_and_duplicate_fields() {
        let credential = CredentialRecord::fixture("https://agent.example/a0");
        let nonce = URL_SAFE_NO_PAD.encode([3; 32]);
        let (auth, _) = signed_auth(
            &credential,
            &nonce,
            &challenge(&credential.server_base_origin, 60_001),
            1,
            BrowserTransportProfile::fixture_production(),
        )
        .unwrap();
        verify_auth(&auth);
        assert_eq!(
            signed_auth(
                &credential,
                &nonce,
                &challenge("https://other.example", 100),
                1,
                BrowserTransportProfile::fixture_production(),
            )
            .err(),
            Some(ConnectError::InvalidChallenge)
        );
        assert_eq!(
            signed_auth(
                &credential,
                &nonce,
                &challenge(&credential.server_base_origin, 1),
                1,
                BrowserTransportProfile::fixture_production(),
            )
            .err(),
            Some(ConnectError::Expired)
        );
        assert!(signed_auth(
            &credential,
            &nonce,
            br#"{"trust_version":1,"trust_version":1}"#,
            1,
            BrowserTransportProfile::fixture_production(),
        )
        .is_err());
        assert!(signed_auth(
            &credential,
            "bad-nonce",
            &challenge(&credential.server_base_origin, 100),
            1,
            BrowserTransportProfile::fixture_production(),
        )
        .is_err());
        let endpoint = Endpoint::parse(&credential.server_base_origin).unwrap();
        assert_eq!(
            endpoint.websocket_url,
            "wss://agent.example/a0/socket.io/?EIO=4&transport=websocket"
        );
        assert_eq!(endpoint.origin, "https://agent.example");
        assert!(Endpoint::parse("http://remote.example").is_err());
    }

    #[test]
    fn unsupported_events_and_inactive_or_partial_readiness_fail_closed() {
        let mut wire = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            fixture_expectation(),
        );
        assert!(wire.receive("40/ws,{}").is_err());
        wire.phase = Phase::Hello;
        wire.namespace_sid = Some("namespace-fixture".into());
        assert!(wire.receive(INACTIVE_ACK).is_err());

        let mut wire = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            fixture_expectation(),
        );
        wire.phase = Phase::Hello;
        wire.namespace_sid = Some("namespace-fixture".into());
        let ack = format!(
            "43/ws,1{}",
            fixture_admitted_ack(&wire.hello, "namespace-fixture").encode()
        );
        wire.receive(&ack).unwrap();
        assert_eq!(wire.receive("2").unwrap().unwrap().as_str(), "3");
        assert!(wire.receive("42/ws,[\"connector_browser_op\",{}]").is_err());
        assert!(wire.receive("42/ws,[\"state_push\",{}]").is_err());
    }

    #[test]
    fn rotation_fallback_requires_exact_namespace_authentication_rejection() {
        for (phase, packet, expected) in [
            (
                Phase::Namespace,
                "44/ws,{\"message\":\"Connection rejected by server\"}",
                ConnectError::AuthenticationRejected,
            ),
            (
                Phase::Hello,
                "44/ws,{\"message\":\"Connection rejected by server\"}",
                ConnectError::Rejected,
            ),
            (
                Phase::Connected,
                "44/ws,{\"message\":\"Connection rejected by server\"}",
                ConnectError::Rejected,
            ),
            (Phase::Namespace, "41/ws,", ConnectError::Rejected),
            (
                Phase::Namespace,
                "44/ws,{\"message\":\"other\"}",
                ConnectError::Rejected,
            ),
            (
                Phase::Namespace,
                "44/ws,{\"message\":\"Connection rejected by server\",\"extra\":true}",
                ConnectError::Rejected,
            ),
            (Phase::Namespace, "44/ws,{", ConnectError::InvalidPacket),
        ] {
            let mut wire = Wire::new(Zeroizing::new("auth".into()), 5_000, fixture_expectation());
            wire.phase = phase;
            assert_eq!(wire.receive(packet).err(), Some(expected));
        }
    }

    #[test]
    fn runtime_refresh_is_correlated_bounded_and_cannot_change_authority() {
        let mut wire = Wire::new(Zeroizing::new("auth".into()), 5_000, fixture_expectation());
        wire.phase = Phase::Hello;
        wire.namespace_sid = Some("namespace-fixture".into());
        let initial = fixture_admitted_ack(&wire.hello, "namespace-fixture");
        wire.receive(&format!("43/ws,1{}", initial.encode()))
            .unwrap();
        wire.next_refresh = Some(Instant::now());
        let refresh = wire.runtime_refresh(Instant::now()).unwrap().unwrap();
        assert!(refresh.contains("bridge-refresh-1"));
        assert!(wire.runtime_refresh(Instant::now()).unwrap().is_none());
        assert!(wire
            .accept_runtime_refresh(&format!("43/ws,1{}", initial.encode()))
            .is_err());
        let ack = fixture_admitted_ack(&wire.hello, "namespace-fixture");
        wire.accept_runtime_refresh(&format!("43/ws,1{}", ack.encode()))
            .unwrap();
        assert!(wire.refresh_deadline.is_none());
        assert!(wire
            .accept_runtime_refresh(&format!("43/ws,1{}", ack.encode()))
            .is_err());
        wire.next_refresh = Some(Instant::now());
        wire.runtime_refresh(Instant::now()).unwrap().unwrap();
        wire.refresh_deadline = Some(Instant::now());
        assert_eq!(
            wire.runtime_refresh(Instant::now()),
            Err(ConnectError::Expired)
        );
    }

    #[test]
    fn artifact_authorizer_requires_the_exact_route_and_current_ready_worker() {
        let expected = fixture_expectation();
        let route = parse_core_hello_ack(
            &fixture_admitted_ack(&expected, "namespace-fixture"),
            &expected,
            "namespace-fixture",
        )
        .unwrap();
        let invocation = crate::native_host::NativeInvocation::fixture(
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
        );
        let artifact_route = route.artifact_route(&invocation).unwrap();
        let connection = CoreConnection::fixture(ConnectionStatus::Ready, None);
        let authorizer = connection.artifact_authorizer(&artifact_route);
        let binding = ArtifactBinding::new(
            &artifact_route,
            "context-1",
            "session-1",
            "turn-1",
            "action-1",
            "op-1",
            "artifact-1",
            crate::artifact::ArtifactDirection::Output,
            crate::artifact::ArtifactPurpose::Screenshot,
        )
        .unwrap();
        assert!(authorizer(&binding));

        connection
            .status
            .store(ConnectionStatus::Failed as u8, Ordering::Release);
        assert!(!authorizer(&binding));
        connection
            .status
            .store(ConnectionStatus::Ready as u8, Ordering::Release);
        let other_route = ArtifactRoute::from_validated_invocation(
            &invocation,
            "install-fixture",
            "generation-fixture",
            "server-fixture",
            "bridge-other",
            1,
            "namespace-fixture",
        )
        .unwrap();
        let other = ArtifactBinding::new(
            &other_route,
            "context-1",
            "session-1",
            "turn-1",
            "action-1",
            "op-1",
            "artifact-1",
            crate::artifact::ArtifactDirection::Output,
            crate::artifact::ArtifactPurpose::Screenshot,
        )
        .unwrap();
        assert!(!authorizer(&other));
    }

    #[test]
    fn real_websocket_signed_namespace_hello_heartbeat_and_cancel() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let credential = CredentialRecord::fixture(&base);
        let expires = now_ms().unwrap() + 60_000;
        let (auth, _) = signed_auth(
            &credential,
            &URL_SAFE_NO_PAD.encode([3; 32]),
            &challenge(&base, expires),
            now_ms().unwrap(),
            BrowserTransportProfile::fixture_production(),
        )
        .unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let status = Arc::new(AtomicU8::new(0));
        let admitted_route = Arc::new(Mutex::new(None));
        let worker_stop = Arc::clone(&stop);
        let worker_status = Arc::clone(&status);
        let hello = fixture_expectation();
        let hello_packet = hello.packet();
        let hello_ack = format!(
            "43/ws,1{}",
            fixture_admitted_ack(&hello, "namespace-fixture").encode()
        );
        let (command_sender, commands) = mpsc::sync_channel(8);
        let (result_sender, results) = mpsc::sync_channel(8);
        let channels = WorkerChannels {
            commands: command_sender,
            results,
            result_deadline: Arc::new(Mutex::new(Instant::now())),
        };
        let relay = std::thread::spawn(move || {
            let command: CoreCommand = commands.recv_timeout(Duration::from_secs(5)).unwrap();
            assert_eq!(command.bridge_id, "bridge-fixture");
            let mut codec =
                crate::connector_codec::ConnectorCodec::new("generation-fixture".into());
            let native = codec.command(&command.packet, &command.bridge_id).unwrap();
            let crate::rpc::RpcMessage::Request(request) =
                crate::rpc::parse_message(&native, crate::rpc::Peer::Server).unwrap()
            else {
                panic!()
            };
            assert_eq!(request.method, "browser.finalize_turn");
            let response = crate::rpc::RpcMessage::Response(crate::rpc::RpcResponse {
                id: request.id.unwrap(),
                result: Ok(object(&[
                    ("contract_version", Value::Number("1".into())),
                    ("control_id", text("control-1")),
                    ("closed", Value::Array(vec![])),
                    ("released", Value::Array(vec![])),
                    ("retained", Value::Array(vec![])),
                    ("already_finalized", Value::Array(vec![])),
                    ("errors", Value::Array(vec![])),
                ])),
            })
            .encode();
            result_sender
                .send(codec.response(&response).unwrap())
                .unwrap();
            // Keep the outbound channel alive until server verifies the result.
            commands.recv_timeout(Duration::from_secs(5)).ok();
        });
        let server = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut socket = tungstenite::accept_hdr(
                stream,
                |request: &tungstenite::handshake::server::Request, response| {
                    assert_eq!(request.uri().path(), "/socket.io/");
                    assert_eq!(request.uri().query(), Some("EIO=4&transport=websocket"));
                    for forbidden in ["Cookie", "Authorization", "X-API-Key"] {
                        assert!(!request.headers().contains_key(forbidden));
                    }
                    assert_eq!(request.headers()["Origin"].to_str().unwrap(), base);
                    Ok(response)
                },
            )
            .unwrap();
            socket.send(Message::Text("0{\"sid\":\"engine-fixture\",\"upgrades\":[],\"pingInterval\":25000,\"pingTimeout\":20000,\"maxPayload\":1000000}".into())).unwrap();
            verify_auth(socket.read().unwrap().to_text().unwrap());
            socket
                .send(Message::Text(
                    "40/ws,{\"sid\":\"namespace-fixture\"}".into(),
                ))
                .unwrap();
            assert_eq!(socket.read().unwrap().to_text().unwrap(), hello_packet);
            socket.send(Message::Text(hello_ack.into())).unwrap();
            socket.send(Message::Text("2".into())).unwrap();
            assert_eq!(socket.read().unwrap().to_text().unwrap(), "3");
            assert_eq!(
                worker_status.load(Ordering::Acquire),
                ConnectionStatus::Ready as u8
            );
            let params = object(&[
                ("method", text("browser.finalize_turn")),
                ("contract_version", Value::Number("1".into())),
                ("bridge_id", text("bridge-fixture")),
                ("load_generation_id", text("generation-fixture")),
                ("context_id", text("context-1")),
                ("browser_session_id", text("session-1")),
                ("turn_id", text("turn-1")),
                ("control_id", text("control-1")),
                ("dispositions", object(&[])),
                ("reason", text("completed")),
            ]);
            let packet = format!(
                "42/ws,{}",
                Value::Array(vec![
                    text("connector_browser_control"),
                    object(&[
                        ("handlerId", text(crate::connector_codec::HANDLER_ID)),
                        ("correlationId", text("control-1")),
                        ("data", params),
                    ])
                ])
                .encode()
            );
            socket.send(Message::Text(packet.into())).unwrap();
            let result = socket.read().unwrap();
            let result = result.to_text().unwrap();
            assert!(result.contains("connector_browser_control_result"));
            assert!(result.contains("\"method\":\"browser.finalize_turn\""));
            assert!(result.contains("\"bridge_id\":\"bridge-fixture\""));
            assert!(result.contains("\"control_id\":\"control-1\""));
            worker_stop.store(true, Ordering::Release);
            assert_eq!(socket.read().unwrap().to_text().unwrap(), "41/ws,");
        });
        assert_eq!(
            serve(
                Endpoint::parse(&credential.server_base_origin).unwrap(),
                auth,
                expires,
                &stop,
                &status,
                &admitted_route,
                Some(&channels),
                hello,
                BrowserTransportProfile::compiled(),
                None,
            ),
            Err(ConnectError::Stopped)
        );
        drop(channels);
        let route = admitted_route.lock().unwrap().take().unwrap();
        assert_eq!(route.connector_sid(), "namespace-fixture");
        assert_eq!(route.key_generation(), 1);
        relay.join().unwrap();
        server.join().unwrap();
    }
}

#[cfg(all(test, feature = "local-development"))]
mod development_tests {
    use super::*;

    fn ack(data: Value) -> String {
        format!(
            "43/ws,1{}",
            Value::Array(vec![object(&[
                ("correlationId", text("bridge-hello")),
                (
                    "results",
                    Value::Array(vec![object(&[
                        (
                            "handlerId",
                            text(crate::development_session::DEVELOPMENT_HANDLER_ID),
                        ),
                        ("ok", Value::Bool(true)),
                        ("correlationId", text("bridge-hello")),
                        ("data", data),
                    ])]),
                ),
            ])])
            .encode()
        )
    }

    fn engine_open(interval: u64, timeout: u64) -> String {
        format!(
            "0{{\"sid\":\"engine-live-fixture\",\"upgrades\":[],\"pingInterval\":{interval},\"pingTimeout\":{timeout},\"maxPayload\":52428800}}"
        )
    }

    #[test]
    fn development_wire_accepts_live_core_heartbeat_and_bounds_engine_durations() {
        let mut wire = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            crate::development_session::fixture_expectation(),
        );
        assert_eq!(
            wire.receive(&engine_open(45_000, 120_000))
                .unwrap()
                .unwrap()
                .as_str(),
            "40/ws,{}"
        );
        assert_eq!(wire.phase, Phase::Namespace);
        assert_eq!(wire.heartbeat, Duration::from_millis(165_000));

        for packet in [
            engine_open(0, 120_000),
            engine_open(60_001, 120_000),
            engine_open(45_000, 0),
            engine_open(45_000, 120_001),
        ] {
            let mut invalid = Wire::new(
                Zeroizing::new("40/ws,{}".into()),
                60_000,
                crate::development_session::fixture_expectation(),
            );
            assert_eq!(invalid.receive(&packet), Err(ConnectError::InvalidPacket));
            assert_eq!(invalid.phase, Phase::EngineOpen);
        }
    }

    fn provisional_reconcile() -> String {
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
        data.insert("bridge_id".into(), text("bridge-dev-fixture"));
        data.insert("load_generation_id".into(), text("load-dev-fixture"));
        format!(
            "42/ws,{}",
            Value::Array(vec![
                text("connector_browser_control"),
                object(&[
                    (
                        "handlerId",
                        text(crate::development_session::DEVELOPMENT_HANDLER_ID),
                    ),
                    ("eventId", text("event-reconcile-dev-1")),
                    ("correlationId", text("reconcile-dev-1")),
                    ("ts", text("2026-09-05T12:34:56.789Z")),
                    ("data", Value::Object(data)),
                ]),
            ])
            .encode()
        )
    }

    #[test]
    fn actual_core_ws_wire_fixture_admits_and_releases_reconciliation() {
        let fixture = crate::json::parse(include_bytes!(
            "../tests/fixtures/limited-development-ws-wire-v1.json"
        ))
        .unwrap();
        let fixture = fixture.as_object().unwrap();
        assert_eq!(
            fixture.get("contract").and_then(Value::as_str),
            Some("a0.browser-bridge.development-ws-wire.v1")
        );
        let reconcile = format!("42/ws,{}", fixture["provisional_reconcile_packet"].encode());
        let hello_ack = format!("43/ws,1{}", fixture["hello_ack_args"].encode());
        let mut wire = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            crate::development_session::fixture_expectation(),
        );
        wire.phase = Phase::Hello;
        wire.namespace_sid = Some("sid-dev-fixture".into());

        assert_eq!(wire.receive(&reconcile), Ok(None));
        assert_eq!(wire.receive(&hello_ack), Ok(None));
        assert_eq!(wire.phase, Phase::Connected);
        assert!(wire.development_route.is_some());

        let retained = wire.take_provisional_reconcile().unwrap();
        let mut codec = crate::connector_codec::ConnectorCodec::with_profile(
            "load-dev-fixture".into(),
            BrowserTransportProfile::fixture_development(),
        );
        assert!(codec.command(&retained, "bridge-dev-fixture").is_ok());
    }

    fn ack_data() -> Value {
        let fixture = crate::json::parse(include_bytes!(
            "../tests/fixtures/development-session-v1.json"
        ))
        .unwrap();
        fixture
            .as_object()
            .unwrap()
            .get("hello_ack_data")
            .unwrap()
            .clone()
    }

    #[test]
    fn development_wire_accepts_only_hello_then_heartbeat() {
        let mut wire = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            crate::development_session::fixture_expectation(),
        );
        wire.phase = Phase::Hello;
        wire.namespace_sid = Some("sid-dev-fixture".into());
        let packet = ack(ack_data());
        assert_eq!(wire.receive(&packet), Ok(None));
        assert!(wire.phase == Phase::Connected);
        assert!(wire.admitted_route.is_none());
        assert_eq!(wire.receive("2").unwrap().unwrap().as_str(), "3");
        assert_eq!(
            wire.receive("42/ws,[\"connector_browser_op\",{}]"),
            Err(ConnectError::InvalidPacket)
        );

        let connection =
            CoreConnection::fixture(ConnectionStatus::AuthenticatedDevelopmentPairingOnly, None);
        assert!(connection.send_result("42/ws,[]".into()).is_err());
        assert!(connection.take_admitted_route().is_none());
    }

    #[test]
    fn development_wire_retains_only_one_bound_reconcile_until_limited_ack() {
        let mut wire = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            crate::development_session::fixture_expectation(),
        );
        wire.phase = Phase::Hello;
        wire.namespace_sid = Some("sid-dev-fixture".into());
        let reconcile = provisional_reconcile();
        assert_eq!(wire.receive(&reconcile), Ok(None));
        assert_eq!(wire.phase, Phase::Hello);
        assert_eq!(wire.receive(&reconcile), Err(ConnectError::InvalidPacket));

        let mut admitted = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            crate::development_session::fixture_expectation(),
        );
        admitted.phase = Phase::Hello;
        admitted.namespace_sid = Some("sid-dev-fixture".into());
        assert_eq!(admitted.receive(&reconcile), Ok(None));
        assert_eq!(
            admitted.receive(&ack(crate::development_session::fixture_limited_ack_data(
                "sid-dev-fixture"
            ))),
            Ok(None)
        );
        assert_eq!(admitted.phase, Phase::Connected);
        assert!(admitted.development_route.is_some());
        let retained = admitted.take_provisional_reconcile().unwrap();
        assert_eq!(retained, reconcile);
        let mut codec = crate::connector_codec::ConnectorCodec::with_profile(
            "load-dev-fixture".into(),
            BrowserTransportProfile::fixture_development(),
        );
        assert!(codec.command(&retained, "bridge-dev-fixture").is_ok());
        assert!(admitted.take_provisional_reconcile().is_none());

        let mut inactive = Wire::new(
            Zeroizing::new("40/ws,{}".into()),
            60_000,
            crate::development_session::fixture_expectation(),
        );
        inactive.phase = Phase::Hello;
        inactive.namespace_sid = Some("sid-dev-fixture".into());
        assert_eq!(inactive.receive(&reconcile), Ok(None));
        assert_eq!(
            inactive.receive(&ack(ack_data())),
            Err(ConnectError::InvalidPacket)
        );
    }

    #[test]
    fn development_wire_rejects_mismatched_or_nonreconcile_pre_ack_packets() {
        for packet in [
            provisional_reconcile().replace("load-dev-fixture", "load-other"),
            provisional_reconcile().replace("browser.reconcile", "browser.finalize_turn"),
            provisional_reconcile().replace(
                crate::development_session::DEVELOPMENT_HANDLER_ID,
                crate::transport_profile::PRODUCTION_HANDLER_ID,
            ),
            provisional_reconcile().replace("event-reconcile-dev-1", ""),
            provisional_reconcile().replace("2026-09-05T12:34:56.789Z", &"x".repeat(65)),
        ] {
            let mut wire = Wire::new(
                Zeroizing::new("40/ws,{}".into()),
                60_000,
                crate::development_session::fixture_expectation(),
            );
            wire.phase = Phase::Hello;
            wire.namespace_sid = Some("sid-dev-fixture".into());
            assert_eq!(wire.receive(&packet), Err(ConnectError::InvalidPacket));
        }
    }
}
