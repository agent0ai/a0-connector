//! Chromium native-messaging invocation and framing.
//!
//! This module never writes unframed data to stdout. The CLI owns a different
//! entry point and is selected only after native-host candidate detection.

use std::io::{self, Read, Write};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{atomic::Ordering, Arc};
use std::time::{Duration, Instant};

use crate::manifest::is_exact_extension_origin;
#[cfg(not(feature = "local-development"))]
use crate::release::{release_trust_configured, PRODUCTION_EXTENSION_ORIGINS};
use crate::rpc::Peer;
use crate::session::{RelaySession, RoutedMessage, SessionError, SessionState};

pub const MAX_INBOUND_FRAME_BYTES: usize = 768 * 1024;
pub const MAX_OUTBOUND_FRAME_BYTES: usize = 768 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvocationError {
    OriginMissing,
    OriginMalformed,
    OriginNotAllowed,
    RuntimeNotActivated,
    InvalidParentWindow,
    UnexpectedArgument,
}

impl InvocationError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::OriginMissing => "NATIVE_ORIGIN_MISSING",
            Self::OriginMalformed => "NATIVE_ORIGIN_MALFORMED",
            Self::OriginNotAllowed => "NATIVE_ORIGIN_NOT_ALLOWED",
            Self::RuntimeNotActivated => "NATIVE_RUNTIME_NOT_ACTIVATED",
            Self::InvalidParentWindow => "NATIVE_PARENT_WINDOW_INVALID",
            Self::UnexpectedArgument => "NATIVE_ARGUMENT_UNEXPECTED",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeInvocation {
    caller_origin: String,
    parent_window: Option<u64>,
}

impl NativeInvocation {
    pub fn caller_origin(&self) -> &str {
        &self.caller_origin
    }

    pub const fn parent_window(&self) -> Option<u64> {
        self.parent_window
    }

    #[cfg(test)]
    pub(crate) fn fixture(caller_origin: &str) -> Self {
        assert!(is_exact_extension_origin(caller_origin));
        Self {
            caller_origin: caller_origin.to_owned(),
            parent_window: None,
        }
    }
}

#[derive(Debug)]
pub enum FrameError {
    Closed,
    Truncated,
    Empty,
    TooLarge,
    Protocol(SessionError),
    ServerTransportUnavailable,
    Io(io::Error),
}

impl FrameError {
    pub const fn reason_code(&self) -> &'static str {
        match self {
            Self::Closed => "NATIVE_PORT_CLOSED",
            Self::Truncated => "NATIVE_FRAME_TRUNCATED",
            Self::Empty => "NATIVE_FRAME_EMPTY",
            Self::TooLarge => "NATIVE_FRAME_TOO_LARGE",
            Self::Protocol(error) => error.reason_code(),
            Self::ServerTransportUnavailable => "NATIVE_SERVER_TRANSPORT_UNAVAILABLE",
            Self::Io(_) => "NATIVE_FRAME_IO_ERROR",
        }
    }

    pub const fn exit_code(&self) -> u8 {
        match self {
            Self::Closed => crate::EXIT_OK,
            Self::Truncated
            | Self::Empty
            | Self::TooLarge
            | Self::Protocol(_)
            | Self::ServerTransportUnavailable
            | Self::Io(_) => crate::EXIT_INTEGRITY_OR_POLICY,
        }
    }
}

pub fn is_native_host_candidate(args: &[String]) -> bool {
    args.first().is_some_and(|argument| {
        argument.starts_with("chrome-extension:") || argument.contains("://")
    }) || args
        .iter()
        .any(|argument| argument.starts_with("--parent-window"))
}

pub fn validate_invocation(args: &[String]) -> Result<NativeInvocation, InvocationError> {
    let Some(origin) = args.first() else {
        return Err(InvocationError::OriginMissing);
    };
    if !is_exact_extension_origin(origin) {
        return Err(InvocationError::OriginMalformed);
    }
    #[cfg(not(feature = "local-development"))]
    {
        if !release_trust_configured() {
            return Err(InvocationError::RuntimeNotActivated);
        }
        if !PRODUCTION_EXTENSION_ORIGINS.contains(&origin.as_str()) {
            return Err(InvocationError::OriginNotAllowed);
        }
    }
    #[cfg(feature = "local-development")]
    if origin != crate::DEVELOPMENT_EXTENSION_ORIGIN {
        return Err(InvocationError::OriginNotAllowed);
    }

    let mut parent_window = None;
    for argument in &args[1..] {
        if let Some(raw) = argument.strip_prefix("--parent-window=") {
            if parent_window.is_some() || raw.is_empty() {
                return Err(InvocationError::InvalidParentWindow);
            }
            parent_window = Some(
                raw.parse::<u64>()
                    .map_err(|_| InvocationError::InvalidParentWindow)?,
            );
        } else {
            return Err(InvocationError::UnexpectedArgument);
        }
    }
    Ok(NativeInvocation {
        caller_origin: origin.clone(),
        parent_window,
    })
}

pub fn read_frame<R: Read>(reader: &mut R) -> Result<Vec<u8>, FrameError> {
    let mut header = [0_u8; 4];
    match reader.read(&mut header[..1]) {
        Ok(0) => return Err(FrameError::Closed),
        Ok(_) => {}
        Err(error) => return Err(FrameError::Io(error)),
    }
    reader
        .read_exact(&mut header[1..])
        .map_err(|_| FrameError::Truncated)?;
    let length = u32::from_ne_bytes(header) as usize;
    if length == 0 {
        return Err(FrameError::Empty);
    }
    if length > MAX_INBOUND_FRAME_BYTES {
        return Err(FrameError::TooLarge);
    }
    let mut payload = vec![0_u8; length];
    reader
        .read_exact(&mut payload)
        .map_err(|_| FrameError::Truncated)?;
    Ok(payload)
}

pub fn write_frame<W: Write>(writer: &mut W, payload: &[u8]) -> Result<(), FrameError> {
    if payload.is_empty() {
        return Err(FrameError::Empty);
    }
    if payload.len() > MAX_OUTBOUND_FRAME_BYTES {
        return Err(FrameError::TooLarge);
    }
    let length = u32::try_from(payload.len()).map_err(|_| FrameError::TooLarge)?;
    writer
        .write_all(&length.to_ne_bytes())
        .and_then(|_| writer.write_all(payload))
        .and_then(|_| writer.flush())
        .map_err(FrameError::Io)
}

pub fn run_native_session<R: Read + Send + 'static, W: Write>(
    invocation: &NativeInvocation,
    mut reader: R,
    writer: &mut W,
) -> Result<(), FrameError> {
    // Only this reader thread blocks on stdin. The owning thread remains free
    // to process Core messages and enforce deadlines with an idle native port.
    // One queued frame bounds memory and preserves backpressure. Never join on
    // shutdown: an OS pipe read may remain blocked until the process exits.
    let (input, frames) = mpsc::sync_channel(1);
    let started = Instant::now();
    let mut session = RelaySession::from_validated_invocation(invocation, 0);
    let input_closed = session.native_input_closed_signal();
    let reader_closed = Arc::clone(&input_closed);
    std::thread::Builder::new()
        .name("a0-native-input".into())
        .spawn(move || loop {
            let frame = read_frame(&mut reader);
            let terminal = frame.is_err();
            if matches!(&frame, Err(FrameError::Closed)) {
                // Wake a bounded outbound-queue wait without waiting for the
                // owner to consume the queued EOF. This grants no new effects.
                reader_closed.store(true, Ordering::Release);
            }
            if input.send(frame).is_err() || terminal {
                break;
            }
        })
        .map_err(FrameError::Io)?;
    let outcome = (|| loop {
        let now_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
        let expired = session.expire(now_ms).map_err(FrameError::Protocol)?;
        route_messages(&mut session, writer, expired)?;
        let incoming = session.poll_core(now_ms).map_err(FrameError::Protocol)?;
        route_messages(&mut session, writer, incoming)?;
        let payload = match frames.recv_timeout(Duration::from_millis(25)) {
            Ok(Ok(payload)) => payload,
            Ok(Err(FrameError::Closed)) | Err(RecvTimeoutError::Disconnected) => {
                session.close();
                return Ok(());
            }
            Ok(Err(error)) => return Err(error),
            Err(RecvTimeoutError::Timeout) => continue,
        };
        let now_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
        let routed = session
            .receive(Peer::Extension, &payload, now_ms)
            .map_err(FrameError::Protocol)?;
        route_messages(&mut session, writer, routed)?;
        session.start_requested_core_connection();
        if session.state() == SessionState::Blocked {
            return Ok(());
        }
    })();
    if input_closed.load(Ordering::Acquire)
        && matches!(&outcome, Err(FrameError::Protocol(SessionError::ConnectorUnavailable)))
    {
        session.close();
        return Ok(());
    }
    outcome
}

fn route_messages<W: Write>(
    session: &mut RelaySession,
    writer: &mut W,
    messages: Vec<RoutedMessage>,
) -> Result<(), FrameError> {
    for message in messages {
        match message.target {
            Peer::Extension => write_frame(writer, &message.payload)?,
            Peer::Server => session
                .send_core(&message.payload)
                .map_err(FrameError::Protocol)?,
        }
    }
    Ok(())
}

pub fn framing_self_test() -> bool {
    let payload = br#"{"type":"self-test"}"#;
    let mut encoded = Vec::new();
    if write_frame(&mut encoded, payload).is_err() {
        return false;
    }
    let mut cursor = io::Cursor::new(encoded);
    read_frame(&mut cursor).is_ok_and(|decoded| decoded == payload)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn framing_uses_native_endian_and_round_trips() {
        let payload = b"{}";
        let mut framed = Vec::new();
        write_frame(&mut framed, payload).expect("frame should encode");
        assert_eq!(&framed[..4], &(payload.len() as u32).to_ne_bytes());
        assert_eq!(read_frame(&mut io::Cursor::new(framed)).unwrap(), payload);
    }

    #[test]
    fn oversized_length_is_rejected_before_payload_allocation() {
        let header = ((MAX_INBOUND_FRAME_BYTES + 1) as u32).to_ne_bytes();
        assert!(matches!(
            read_frame(&mut io::Cursor::new(header)),
            Err(FrameError::TooLarge)
        ));
    }

    #[test]
    fn outbound_frames_use_the_same_768_kib_limit() {
        let maximum = vec![b'x'; MAX_OUTBOUND_FRAME_BYTES];
        assert!(write_frame(&mut Vec::new(), &maximum).is_ok());
        let oversized = vec![b'x'; MAX_OUTBOUND_FRAME_BYTES + 1];
        assert!(matches!(
            write_frame(&mut Vec::new(), &oversized),
            Err(FrameError::TooLarge)
        ));
    }

    #[test]
    fn clean_eof_is_success_but_malformed_frames_are_integrity_failures() {
        let closed = read_frame(&mut io::Cursor::new(Vec::<u8>::new())).unwrap_err();
        assert!(matches!(closed, FrameError::Closed));
        assert_eq!(closed.exit_code(), crate::EXIT_OK);

        let truncated_header = read_frame(&mut io::Cursor::new(vec![1_u8])).unwrap_err();
        assert!(matches!(truncated_header, FrameError::Truncated));
        assert_eq!(
            truncated_header.exit_code(),
            crate::EXIT_INTEGRITY_OR_POLICY
        );

        let empty = read_frame(&mut io::Cursor::new(0_u32.to_ne_bytes())).unwrap_err();
        assert!(matches!(empty, FrameError::Empty));
        assert_eq!(empty.exit_code(), crate::EXIT_INTEGRITY_OR_POLICY);

        let oversized = read_frame(&mut io::Cursor::new(
            ((MAX_INBOUND_FRAME_BYTES + 1) as u32).to_ne_bytes(),
        ))
        .unwrap_err();
        assert!(matches!(oversized, FrameError::TooLarge));
        assert_eq!(oversized.exit_code(), crate::EXIT_INTEGRITY_OR_POLICY);

        let mut truncated_payload = 2_u32.to_ne_bytes().to_vec();
        truncated_payload.push(b'{');
        let truncated_payload = read_frame(&mut io::Cursor::new(truncated_payload)).unwrap_err();
        assert!(matches!(truncated_payload, FrameError::Truncated));
        assert_eq!(
            truncated_payload.exit_code(),
            crate::EXIT_INTEGRITY_OR_POLICY
        );
    }

    #[cfg(not(feature = "local-development"))]
    #[test]
    fn configured_release_trust_still_rejects_exact_fixture_origin() {
        let result = validate_invocation(&[
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/".to_owned()
        ]);
        assert_eq!(result, Err(InvocationError::OriginNotAllowed));
    }

    #[cfg(feature = "local-development")]
    #[test]
    fn development_build_accepts_only_its_compiled_extension_origin() {
        assert!(validate_invocation(&[crate::DEVELOPMENT_EXTENSION_ORIGIN.to_owned()]).is_ok());
        assert_eq!(
            validate_invocation(&[
                "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/".to_owned()
            ]),
            Err(InvocationError::OriginNotAllowed)
        );
    }

    #[test]
    fn inactive_server_negotiates_only_a_framed_unpaired_session() {
        #[cfg(not(feature = "local-development"))]
        const ORIGIN: &str = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/";
        #[cfg(not(feature = "local-development"))]
        const EXTENSION_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        #[cfg(feature = "local-development")]
        const ORIGIN: &str = crate::DEVELOPMENT_EXTENSION_ORIGIN;
        #[cfg(feature = "local-development")]
        const EXTENSION_ID: &str = crate::DEVELOPMENT_EXTENSION_ID;
        let invocation = NativeInvocation::fixture(ORIGIN);
        let hello = format!(
            r#"{{"jsonrpc":"2.0","id":"hello-1","method":"bridge.hello","params":{{"protocol":"a0.browser-bridge","contract":{{"min":1,"max":1}},"extension":{{"id":"{EXTENSION_ID}","version":"0.1.0","manifest_version":3,"install_instance_id":"install-1","load_generation_id":"generation-1"}},"browser":{{"family":"chrome","version":"146.0.0.0"}},"capabilities":{{"actions":["open"],"features":["tab_leases_v1"],"cdp_domains":[]}},"resume":{{"event_cursors":[],"inflight_op_ids":[],"lease_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}}}}}}"#
        );
        let mut input = Vec::new();
        write_frame(&mut input, hello.as_bytes()).unwrap();
        let mut output = Vec::new();
        run_native_session(&invocation, io::Cursor::new(input), &mut output).unwrap();
        let response = read_frame(&mut io::Cursor::new(output)).unwrap();
        let response = std::str::from_utf8(&response).unwrap();
        assert!(response.starts_with("{\"id\":\"hello-1\""));
        assert!(response.contains("\"jsonrpc\":\"2.0\""));
        assert!(response.contains("\"state\":\"unpaired\""));
        #[cfg(feature = "local-development")]
        {
            assert!(response.contains("\"contract\":\"a0.browser-bridge.development-trust.v1\""));
            assert!(response.contains("\"reason_code\":\"development_runtime_not_available\""));
            assert!(!response.contains("\"activation\""));
        }
    }
}
