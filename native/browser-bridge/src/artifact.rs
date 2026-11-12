//! Private, bounded artifact spooling for one validated native invocation.
//!
//! This state machine is transport-independent and does not activate browser
//! control. Production construction requires both a `NativeInvocation`-derived
//! route and an injected current-route authorizer. Windows additionally requires
//! verified protected owner/SYSTEM ACLs and retained non-reparse local handles.

use std::collections::{BTreeMap, VecDeque};
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use sha2::{Digest, Sha256};

use crate::native_host::NativeInvocation;
use crate::rpc;

#[cfg(windows)]
#[path = "artifact_windows.rs"]
mod windows_private;

pub const MAX_ARTIFACT_BYTES: u64 = 25 * 1024 * 1024;
pub const MAX_CHUNK_BYTES: usize = 192 * 1024;
pub const DEFAULT_TTL_MS: u64 = 120_000;
pub const DEFAULT_TOMBSTONE_TTL_MS: u64 = 300_000;
pub const DEFAULT_MAX_ARTIFACTS: usize = 16;
pub const DEFAULT_MAX_TOTAL_SPOOL_BYTES: u64 = 100 * 1024 * 1024;
pub const DEFAULT_MAX_TOMBSTONES: usize = 2_048;

const MAX_IDENTIFIER_BYTES: usize = 256;
const MAX_MIME_TYPE_BYTES: usize = 256;
const HARD_MAX_ARTIFACTS: usize = 128;
const HARD_MAX_TOTAL_SPOOL_BYTES: u64 = 256 * 1024 * 1024;
const HARD_MAX_TOMBSTONES: usize = 8_192;
const HARD_MAX_TTL_MS: u64 = 10 * 60 * 1_000;
const RANDOM_NAME_BYTES: usize = 16;
const CREATE_ATTEMPTS: usize = 32;

pub type CurrentRouteAuthorizer = Arc<dyn Fn(&ArtifactBinding) -> bool + Send + Sync + 'static>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ArtifactDirection {
    Output,
    Input,
}

impl ArtifactDirection {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Output => "output",
            Self::Input => "input",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ArtifactPurpose {
    Screenshot,
    Download,
    UploadFile,
}

impl ArtifactPurpose {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Screenshot => "screenshot",
            Self::Download => "download",
            Self::UploadFile => "upload_file",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactRoute {
    caller_origin: String,
    install_instance_id: String,
    load_generation_id: String,
    server_instance_id: String,
    bridge_id: String,
    key_generation: u32,
    connector_sid: String,
}

impl ArtifactRoute {
    #[allow(clippy::too_many_arguments)]
    pub fn from_validated_invocation(
        invocation: &NativeInvocation,
        install_instance_id: &str,
        load_generation_id: &str,
        server_instance_id: &str,
        bridge_id: &str,
        key_generation: u32,
        connector_sid: &str,
    ) -> Result<Self, ArtifactError> {
        for value in [
            install_instance_id,
            load_generation_id,
            server_instance_id,
            bridge_id,
            connector_sid,
        ] {
            validate_identifier(value)?;
        }
        if key_generation == 0 {
            return Err(ArtifactError::InvalidBinding);
        }
        Ok(Self {
            caller_origin: invocation.caller_origin().to_owned(),
            install_instance_id: install_instance_id.to_owned(),
            load_generation_id: load_generation_id.to_owned(),
            server_instance_id: server_instance_id.to_owned(),
            bridge_id: bridge_id.to_owned(),
            key_generation,
            connector_sid: connector_sid.to_owned(),
        })
    }

    pub fn caller_origin(&self) -> &str {
        &self.caller_origin
    }

    pub fn install_instance_id(&self) -> &str {
        &self.install_instance_id
    }

    pub fn load_generation_id(&self) -> &str {
        &self.load_generation_id
    }

    pub fn server_instance_id(&self) -> &str {
        &self.server_instance_id
    }

    pub fn bridge_id(&self) -> &str {
        &self.bridge_id
    }

    pub const fn key_generation(&self) -> u32 {
        self.key_generation
    }

    pub fn connector_sid(&self) -> &str {
        &self.connector_sid
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactBinding {
    route: ArtifactRoute,
    context_id: String,
    browser_session_id: String,
    turn_id: String,
    action_id: String,
    op_id: String,
    artifact_id: String,
    direction: ArtifactDirection,
    purpose: ArtifactPurpose,
}

impl ArtifactBinding {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        route: &ArtifactRoute,
        context_id: &str,
        browser_session_id: &str,
        turn_id: &str,
        action_id: &str,
        op_id: &str,
        artifact_id: &str,
        direction: ArtifactDirection,
        purpose: ArtifactPurpose,
    ) -> Result<Self, ArtifactError> {
        for value in [
            context_id,
            browser_session_id,
            turn_id,
            action_id,
            op_id,
            artifact_id,
        ] {
            validate_identifier(value)?;
        }
        if !matches!(
            (direction, purpose),
            (
                ArtifactDirection::Output,
                ArtifactPurpose::Screenshot | ArtifactPurpose::Download
            ) | (ArtifactDirection::Input, ArtifactPurpose::UploadFile)
        ) {
            return Err(ArtifactError::InvalidPurpose);
        }
        Ok(Self {
            route: route.clone(),
            context_id: context_id.to_owned(),
            browser_session_id: browser_session_id.to_owned(),
            turn_id: turn_id.to_owned(),
            action_id: action_id.to_owned(),
            op_id: op_id.to_owned(),
            artifact_id: artifact_id.to_owned(),
            direction,
            purpose,
        })
    }

    pub const fn contract_version(&self) -> u8 {
        1
    }

    pub const fn route(&self) -> &ArtifactRoute {
        &self.route
    }

    pub fn context_id(&self) -> &str {
        &self.context_id
    }

    pub fn browser_session_id(&self) -> &str {
        &self.browser_session_id
    }

    pub fn turn_id(&self) -> &str {
        &self.turn_id
    }

    pub fn action_id(&self) -> &str {
        &self.action_id
    }

    pub fn op_id(&self) -> &str {
        &self.op_id
    }

    pub fn artifact_id(&self) -> &str {
        &self.artifact_id
    }

    pub const fn direction(&self) -> ArtifactDirection {
        self.direction
    }

    pub const fn purpose(&self) -> ArtifactPurpose {
        self.purpose
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ArtifactError {
    InvalidBinding,
    InvalidPurpose,
    InvalidLimits,
    ClockInvalid,
    ScopeDenied,
    ScopeDeniedAndAborted,
    ArtifactIdReused,
    IdempotencyConflict,
    RegistryFull,
    SpoolFull,
    ArtifactSizeInvalid,
    ArtifactDigestInvalid,
    ArtifactMimeInvalid,
    ChunkOutOfOrder,
    ChunkSizeInvalid,
    ArtifactSizeMismatch,
    ArtifactIntegrityMismatch,
    ArtifactAlreadyComplete,
    ArtifactNotComplete,
    ArtifactNotFound,
    ArtifactTerminal,
    PrivateSpoolUnavailable,
    SpoolIo,
    SpoolIoAndAborted,
    ClockInvalidAndAborted,
    Closed,
}

impl ArtifactError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::InvalidBinding => "ARTIFACT_BINDING_INVALID",
            Self::InvalidPurpose => "ARTIFACT_PURPOSE_INVALID",
            Self::InvalidLimits => "ARTIFACT_LIMITS_INVALID",
            Self::ClockInvalid | Self::ClockInvalidAndAborted => "NATIVE_CLOCK_INVALID",
            Self::ScopeDenied | Self::ScopeDeniedAndAborted => "SCOPE_DENIED",
            Self::ArtifactIdReused => "ARTIFACT_ID_REUSED",
            Self::IdempotencyConflict => "ARTIFACT_IDEMPOTENCY_CONFLICT",
            Self::RegistryFull => "ARTIFACT_REGISTRY_FULL",
            Self::SpoolFull => "ARTIFACT_SPOOL_FULL",
            Self::ArtifactSizeInvalid => "ARTIFACT_SIZE_INVALID",
            Self::ArtifactDigestInvalid => "ARTIFACT_DIGEST_INVALID",
            Self::ArtifactMimeInvalid => "ARTIFACT_MIME_INVALID",
            Self::ChunkOutOfOrder => "ARTIFACT_CHUNK_OUT_OF_ORDER",
            Self::ChunkSizeInvalid => "ARTIFACT_CHUNK_SIZE_INVALID",
            Self::ArtifactSizeMismatch => "ARTIFACT_SIZE_MISMATCH",
            Self::ArtifactIntegrityMismatch => "ARTIFACT_INTEGRITY_MISMATCH",
            Self::ArtifactAlreadyComplete => "ARTIFACT_ALREADY_COMPLETE",
            Self::ArtifactNotComplete => "ARTIFACT_NOT_COMPLETE",
            Self::ArtifactNotFound => "ARTIFACT_NOT_FOUND",
            Self::ArtifactTerminal => "ARTIFACT_TERMINAL",
            Self::PrivateSpoolUnavailable => "PRIVATE_ARTIFACT_SPOOL_UNAVAILABLE",
            Self::SpoolIo | Self::SpoolIoAndAborted => "ARTIFACT_SPOOL_IO_FAILED",
            Self::Closed => "ARTIFACT_RECEIVER_CLOSED",
        }
    }

    pub const fn aborted(self) -> bool {
        matches!(
            self,
            Self::ScopeDeniedAndAborted
                | Self::ChunkOutOfOrder
                | Self::ChunkSizeInvalid
                | Self::ArtifactSizeMismatch
                | Self::ArtifactIntegrityMismatch
                | Self::SpoolIoAndAborted
                | Self::ClockInvalidAndAborted
        )
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ArtifactLimits {
    pub ttl_ms: u64,
    pub tombstone_ttl_ms: u64,
    pub max_artifacts: usize,
    pub max_total_spool_bytes: u64,
    pub max_tombstones: usize,
}

impl Default for ArtifactLimits {
    fn default() -> Self {
        Self {
            ttl_ms: DEFAULT_TTL_MS,
            tombstone_ttl_ms: DEFAULT_TOMBSTONE_TTL_MS,
            max_artifacts: DEFAULT_MAX_ARTIFACTS,
            max_total_spool_bytes: DEFAULT_MAX_TOTAL_SPOOL_BYTES,
            max_tombstones: DEFAULT_MAX_TOMBSTONES,
        }
    }
}

impl ArtifactLimits {
    fn validate(self) -> Result<Self, ArtifactError> {
        if !(1..=HARD_MAX_TTL_MS).contains(&self.ttl_ms)
            || !(1..=HARD_MAX_TTL_MS).contains(&self.tombstone_ttl_ms)
            || !(1..=HARD_MAX_ARTIFACTS).contains(&self.max_artifacts)
            || !(1..=HARD_MAX_TOTAL_SPOOL_BYTES).contains(&self.max_total_spool_bytes)
            || !(1..=HARD_MAX_TOMBSTONES).contains(&self.max_tombstones)
        {
            return Err(ArtifactError::InvalidLimits);
        }
        Ok(self)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactProgress {
    pub artifact_id: String,
    pub status: &'static str,
    pub next_chunk_index: u64,
    pub received_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ArtifactDescriptor {
    artifact_id: String,
    mime_type: String,
    byte_count: u64,
    sha256: String,
    purpose: ArtifactPurpose,
}

impl ArtifactDescriptor {
    pub fn artifact_id(&self) -> &str {
        &self.artifact_id
    }

    pub fn mime_type(&self) -> &str {
        &self.mime_type
    }

    pub const fn byte_count(&self) -> u64 {
        self.byte_count
    }

    pub fn sha256(&self) -> &str {
        &self.sha256
    }

    pub const fn purpose(&self) -> ArtifactPurpose {
        self.purpose
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ArtifactCleanupSummary {
    pub artifacts: usize,
    pub reserved_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TransferState {
    Receiving,
    Complete,
    Consuming,
}

struct Transfer {
    binding: ArtifactBinding,
    byte_count: u64,
    sha256: String,
    expected_digest: [u8; 32],
    mime_type: String,
    path: PathBuf,
    file: File,
    digest: Sha256,
    received_bytes: u64,
    next_chunk_index: u64,
    state: TransferState,
    expires_at_ms: u64,
    descriptor: Option<ArtifactDescriptor>,
}

#[derive(Clone)]
struct Tombstone {
    binding: ArtifactBinding,
    expires_at_ms: u64,
}

pub struct ArtifactSpool {
    root: PathBuf,
    #[cfg(windows)]
    root_guards: Vec<File>,
    authorizer: CurrentRouteAuthorizer,
    limits: ArtifactLimits,
    transfers: BTreeMap<String, Transfer>,
    tombstones: BTreeMap<String, Tombstone>,
    tombstone_order: VecDeque<String>,
    reserved_bytes: u64,
    last_now_ms: u64,
    closed: bool,
}

impl ArtifactSpool {
    pub fn new(
        spool_parent: Option<&Path>,
        authorizer: CurrentRouteAuthorizer,
        started_at_ms: u64,
        limits: ArtifactLimits,
    ) -> Result<Self, ArtifactError> {
        let limits = limits.validate()?;
        let parent = private_spool_parent(spool_parent)?;
        let root = create_private_root(&parent)?;
        #[cfg(windows)]
        let root_guards = windows_private::guard_chain(&root)?;
        Ok(Self {
            root,
            #[cfg(windows)]
            root_guards,
            authorizer,
            limits,
            transfers: BTreeMap::new(),
            tombstones: BTreeMap::new(),
            tombstone_order: VecDeque::new(),
            reserved_bytes: 0,
            last_now_ms: started_at_ms,
            closed: false,
        })
    }

    pub fn active_count(&self) -> usize {
        self.transfers.len()
    }

    pub const fn reserved_bytes(&self) -> u64 {
        self.reserved_bytes
    }

    pub(crate) fn contains_exact(&self, binding: &ArtifactBinding) -> bool {
        self.transfers
            .get(binding.artifact_id())
            .is_some_and(|transfer| transfer.binding == *binding)
    }

    pub fn begin(
        &mut self,
        binding: &ArtifactBinding,
        byte_count: u64,
        sha256: &str,
        mime_type: &str,
        now_ms: u64,
    ) -> Result<ArtifactProgress, ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        if byte_count > MAX_ARTIFACT_BYTES {
            return Err(ArtifactError::ArtifactSizeInvalid);
        }
        let expected_digest = parse_sha256(sha256)?;
        validate_mime_type(mime_type)?;
        self.authorize_request(binding, now_ms)?;
        if self.tombstones.contains_key(binding.artifact_id()) {
            return Err(ArtifactError::ArtifactIdReused);
        }
        if let Some(existing) = self.transfers.get(binding.artifact_id()) {
            if existing.byte_count != byte_count
                || existing.sha256 != sha256
                || existing.mime_type != mime_type
            {
                return Err(ArtifactError::IdempotencyConflict);
            }
            return Ok(progress(existing, "duplicate"));
        }
        if self.transfers.len() >= self.limits.max_artifacts {
            return Err(ArtifactError::RegistryFull);
        }
        if self
            .reserved_bytes
            .checked_add(byte_count)
            .filter(|total| *total <= self.limits.max_total_spool_bytes)
            .is_none()
        {
            return Err(ArtifactError::SpoolFull);
        }
        let expires_at_ms = now_ms
            .checked_add(self.limits.ttl_ms)
            .ok_or(ArtifactError::ClockInvalid)?;
        let suffix = if binding.direction == ArtifactDirection::Input {
            input_mime_suffix(mime_type)
        } else {
            ""
        };
        let (path, file) = create_private_file(&self.root, suffix)?;
        let transfer = Transfer {
            binding: binding.clone(),
            byte_count,
            sha256: sha256.to_owned(),
            expected_digest,
            mime_type: mime_type.to_owned(),
            path,
            file,
            digest: Sha256::new(),
            received_bytes: 0,
            next_chunk_index: 0,
            state: TransferState::Receiving,
            expires_at_ms,
            descriptor: None,
        };
        self.reserved_bytes += byte_count;
        self.transfers
            .insert(binding.artifact_id().to_owned(), transfer);
        Ok(progress(
            self.transfers
                .get(binding.artifact_id())
                .expect("inserted transfer"),
            "accepted",
        ))
    }

    pub fn append(
        &mut self,
        binding: &ArtifactBinding,
        chunk_index: u64,
        data: &[u8],
        now_ms: u64,
    ) -> Result<ArtifactProgress, ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        self.authorize_request(binding, now_ms)?;
        let id = binding.artifact_id().to_owned();
        let transfer = self.transfer(binding)?;
        if transfer.state != TransferState::Receiving {
            return Err(ArtifactError::ArtifactAlreadyComplete);
        }
        if chunk_index != transfer.next_chunk_index {
            self.abort_id(&id, now_ms, true);
            return Err(ArtifactError::ChunkOutOfOrder);
        }
        if data.is_empty() || data.len() > MAX_CHUNK_BYTES {
            self.abort_id(&id, now_ms, true);
            return Err(ArtifactError::ChunkSizeInvalid);
        }
        let data_len = u64::try_from(data.len()).map_err(|_| ArtifactError::ChunkSizeInvalid)?;
        if transfer
            .received_bytes
            .checked_add(data_len)
            .filter(|total| *total <= transfer.byte_count)
            .is_none()
        {
            self.abort_id(&id, now_ms, true);
            return Err(ArtifactError::ArtifactSizeMismatch);
        }
        let result = transfer.file.write(data);
        if !matches!(result, Ok(written) if written == data.len()) {
            self.abort_id(&id, now_ms, true);
            return Err(ArtifactError::SpoolIoAndAborted);
        }
        let transfer = self
            .transfers
            .get_mut(&id)
            .expect("validated transfer remains live");
        transfer.digest.update(data);
        transfer.received_bytes += data_len;
        transfer.next_chunk_index += 1;
        Ok(progress(transfer, "accepted"))
    }

    pub fn complete(
        &mut self,
        binding: &ArtifactBinding,
        now_ms: u64,
    ) -> Result<ArtifactDescriptor, ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        self.authorize_request(binding, now_ms)?;
        let id = binding.artifact_id().to_owned();
        let ttl_ms = self.limits.ttl_ms;
        let verified = {
            let transfer = self.transfer(binding)?;
            if matches!(
                transfer.state,
                TransferState::Complete | TransferState::Consuming
            ) {
                return transfer
                    .descriptor
                    .clone()
                    .ok_or(ArtifactError::ArtifactIntegrityMismatch);
            }
            let actual_digest: [u8; 32] = transfer.digest.clone().finalize().into();
            match transfer.file.metadata() {
                Err(_) => Err(ArtifactError::SpoolIoAndAborted),
                Ok(metadata)
                    if transfer.received_bytes != transfer.byte_count
                        || metadata.len() != transfer.byte_count
                        || actual_digest != transfer.expected_digest =>
                {
                    Err(ArtifactError::ArtifactIntegrityMismatch)
                }
                Ok(_) if transfer.file.sync_all().is_err() => Err(ArtifactError::SpoolIoAndAborted),
                Ok(_) => Ok((
                    transfer.mime_type.clone(),
                    transfer.byte_count,
                    transfer.sha256.clone(),
                )),
            }
        };
        let (mime_type, byte_count, sha256) = match verified {
            Ok(verified) => verified,
            Err(error) => {
                self.abort_id(&id, now_ms, true);
                return Err(error);
            }
        };
        let expires_at_ms = match now_ms.checked_add(ttl_ms) {
            Some(expires_at_ms) => expires_at_ms,
            None => {
                self.abort_id(&id, now_ms, true);
                return Err(ArtifactError::ClockInvalidAndAborted);
            }
        };
        let descriptor = ArtifactDescriptor {
            artifact_id: id.clone(),
            mime_type,
            byte_count,
            sha256,
            purpose: binding.purpose(),
        };
        let transfer = self
            .transfers
            .get_mut(&id)
            .expect("validated transfer remains live");
        transfer.state = TransferState::Complete;
        transfer.expires_at_ms = expires_at_ms;
        transfer.descriptor = Some(descriptor.clone());
        Ok(descriptor)
    }

    /// Reveal only a verified private input spool to its exact operation.
    /// The file remains reserved until terminal operation cleanup or expiry.
    pub(crate) fn input_path(
        &mut self,
        binding: &ArtifactBinding,
        now_ms: u64,
    ) -> Result<(PathBuf, ArtifactDescriptor), ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        self.authorize_request(binding, now_ms)?;
        if binding.direction != ArtifactDirection::Input
            || binding.purpose != ArtifactPurpose::UploadFile
        {
            return Err(ArtifactError::InvalidPurpose);
        }
        #[cfg(not(any(unix, windows)))]
        return Err(ArtifactError::PrivateSpoolUnavailable);
        #[cfg(any(unix, windows))]
        {
            #[cfg(unix)]
            use std::os::unix::fs::MetadataExt;
            let transfer = self.transfer(binding)?;
            if transfer.state != TransferState::Complete {
                return Err(ArtifactError::ArtifactNotComplete);
            }
            #[cfg(windows)]
            windows_private::verify_input(&transfer.file, &transfer.path, transfer.byte_count)?;
            #[cfg(unix)]
            let retained = transfer
                .file
                .metadata()
                .map_err(|_| ArtifactError::SpoolIo)?;
            #[cfg(unix)]
            let named = fs::symlink_metadata(&transfer.path).map_err(|_| ArtifactError::SpoolIo)?;
            #[cfg(unix)]
            if !named.is_file()
                || named.file_type().is_symlink()
                || (retained.dev(), retained.ino(), retained.len())
                    != (named.dev(), named.ino(), named.len())
                || retained.len() != transfer.byte_count
            {
                return Err(ArtifactError::ArtifactIntegrityMismatch);
            }
            transfer
                .file
                .seek(SeekFrom::Start(0))
                .map_err(|_| ArtifactError::SpoolIo)?;
            let mut digest = Sha256::new();
            let mut chunk = [0u8; MAX_CHUNK_BYTES];
            let mut bytes_read = 0u64;
            loop {
                let count = transfer
                    .file
                    .read(&mut chunk)
                    .map_err(|_| ArtifactError::SpoolIo)?;
                if count == 0 {
                    break;
                }
                bytes_read = bytes_read
                    .checked_add(count as u64)
                    .ok_or(ArtifactError::ArtifactIntegrityMismatch)?;
                if bytes_read > transfer.byte_count {
                    return Err(ArtifactError::ArtifactIntegrityMismatch);
                }
                digest.update(&chunk[..count]);
            }
            if bytes_read != transfer.byte_count
                || <[u8; 32]>::from(digest.finalize()) != transfer.expected_digest
            {
                return Err(ArtifactError::ArtifactIntegrityMismatch);
            }
            let result = (
                transfer.path.clone(),
                transfer
                    .descriptor
                    .clone()
                    .ok_or(ArtifactError::ArtifactNotComplete)?,
            );
            self.authorize_request(binding, now_ms)?;
            Ok(result)
        }
    }

    pub fn consume<T, F>(
        &mut self,
        binding: &ArtifactBinding,
        now_ms: u64,
        consumer: F,
    ) -> Result<T, ArtifactError>
    where
        F: FnOnce(&mut File, &ArtifactDescriptor) -> Result<T, ArtifactError>,
    {
        self.observe(now_ms)?;
        self.ensure_open()?;
        self.authorize_request(binding, now_ms)?;
        let id = binding.artifact_id().to_owned();
        let validation = {
            let transfer = self.transfer(binding)?;
            if transfer.state != TransferState::Complete {
                return Err(ArtifactError::ArtifactNotComplete);
            }
            match transfer.file.metadata() {
                Err(_) => Err(ArtifactError::SpoolIoAndAborted),
                Ok(metadata) if metadata.len() != transfer.byte_count => {
                    Err(ArtifactError::ArtifactIntegrityMismatch)
                }
                Ok(_) if transfer.file.seek(SeekFrom::Start(0)).is_err() => {
                    Err(ArtifactError::SpoolIoAndAborted)
                }
                Ok(_) => Ok(()),
            }
        };
        if let Err(error) = validation {
            self.abort_id(&id, now_ms, true);
            return Err(error);
        }
        // The file was opened privately at begin. Recheck live route authority
        // at the final boundary immediately before handing its handle to code.
        if !(self.authorizer)(binding) {
            self.abort_id(&id, now_ms, true);
            return Err(ArtifactError::ScopeDeniedAndAborted);
        }
        let outcome = {
            let transfer = self
                .transfers
                .get_mut(&id)
                .expect("validated transfer remains live");
            transfer.state = TransferState::Consuming;
            let descriptor = transfer
                .descriptor
                .clone()
                .expect("complete transfer has descriptor");
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                consumer(&mut transfer.file, &descriptor)
            }))
        };
        self.abort_id(&id, now_ms, true);
        match outcome {
            Ok(result) => result,
            Err(payload) => std::panic::resume_unwind(payload),
        }
    }

    pub fn abort(&mut self, binding: &ArtifactBinding, now_ms: u64) -> Result<bool, ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        self.expire_inner(now_ms);
        let Some(transfer) = self.transfers.get(binding.artifact_id()) else {
            if let Some(tombstone) = self.tombstones.get(binding.artifact_id()) {
                if tombstone.binding != *binding {
                    return Err(ArtifactError::ScopeDenied);
                }
            }
            return Ok(false);
        };
        if transfer.binding != *binding {
            return Err(ArtifactError::ScopeDenied);
        }
        self.abort_id(binding.artifact_id(), now_ms, true);
        Ok(true)
    }

    pub fn expire(&mut self, now_ms: u64) -> Result<ArtifactCleanupSummary, ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        Ok(self.expire_inner(now_ms))
    }

    pub fn disconnect(
        &mut self,
        route: &ArtifactRoute,
        now_ms: u64,
    ) -> Result<ArtifactCleanupSummary, ArtifactError> {
        self.observe(now_ms)?;
        self.ensure_open()?;
        let ids = self
            .transfers
            .iter()
            .filter(|(_, transfer)| transfer.binding.route() == route)
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        Ok(self.cleanup_ids(ids, now_ms, true))
    }

    pub fn revoke_bridge(
        &mut self,
        bridge_id: &str,
        key_generation: u32,
        now_ms: u64,
    ) -> Result<ArtifactCleanupSummary, ArtifactError> {
        validate_identifier(bridge_id)?;
        if key_generation == 0 {
            return Err(ArtifactError::InvalidBinding);
        }
        self.observe(now_ms)?;
        self.ensure_open()?;
        let ids = self
            .transfers
            .iter()
            .filter(|(_, transfer)| {
                transfer.binding.route().bridge_id() == bridge_id
                    && transfer.binding.route().key_generation() == key_generation
            })
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        Ok(self.cleanup_ids(ids, now_ms, true))
    }

    pub fn close(&mut self) -> ArtifactCleanupSummary {
        if self.closed {
            return ArtifactCleanupSummary::default();
        }
        let ids = self.transfers.keys().cloned().collect::<Vec<_>>();
        let summary = self.cleanup_ids(ids, self.last_now_ms, false);
        self.tombstones.clear();
        self.tombstone_order.clear();
        self.closed = true;
        #[cfg(windows)]
        self.root_guards.clear();
        let _ = fs::remove_dir(&self.root);
        summary
    }

    fn observe(&mut self, now_ms: u64) -> Result<(), ArtifactError> {
        if now_ms < self.last_now_ms {
            return Err(ArtifactError::ClockInvalid);
        }
        self.last_now_ms = now_ms;
        Ok(())
    }

    fn ensure_open(&self) -> Result<(), ArtifactError> {
        if self.closed {
            Err(ArtifactError::Closed)
        } else {
            Ok(())
        }
    }

    fn authorize_request(
        &mut self,
        binding: &ArtifactBinding,
        now_ms: u64,
    ) -> Result<(), ArtifactError> {
        let active = self.transfers.get(binding.artifact_id());
        if active.is_some_and(|transfer| transfer.binding != *binding)
            || self
                .tombstones
                .get(binding.artifact_id())
                .is_some_and(|tombstone| tombstone.binding != *binding)
        {
            return Err(ArtifactError::ScopeDenied);
        }
        if !(self.authorizer)(binding) {
            if active.is_some() {
                self.abort_id(binding.artifact_id(), now_ms, true);
                return Err(ArtifactError::ScopeDeniedAndAborted);
            }
            return Err(ArtifactError::ScopeDenied);
        }
        self.expire_inner(now_ms);
        Ok(())
    }

    fn transfer(&mut self, binding: &ArtifactBinding) -> Result<&mut Transfer, ArtifactError> {
        let Some(transfer) = self.transfers.get_mut(binding.artifact_id()) else {
            if let Some(tombstone) = self.tombstones.get(binding.artifact_id()) {
                if tombstone.binding != *binding {
                    return Err(ArtifactError::ScopeDenied);
                }
                return Err(ArtifactError::ArtifactTerminal);
            }
            return Err(ArtifactError::ArtifactNotFound);
        };
        if transfer.binding != *binding {
            return Err(ArtifactError::ScopeDenied);
        }
        Ok(transfer)
    }

    fn expire_inner(&mut self, now_ms: u64) -> ArtifactCleanupSummary {
        let ids = self
            .transfers
            .iter()
            .filter(|(_, transfer)| transfer.expires_at_ms <= now_ms)
            .map(|(id, _)| id.clone())
            .collect::<Vec<_>>();
        let summary = self.cleanup_ids(ids, now_ms, true);
        self.prune_tombstones(now_ms);
        summary
    }

    fn cleanup_ids(
        &mut self,
        ids: Vec<String>,
        now_ms: u64,
        remember: bool,
    ) -> ArtifactCleanupSummary {
        let mut summary = ArtifactCleanupSummary::default();
        for id in ids {
            let Some(transfer) = self.transfers.remove(&id) else {
                continue;
            };
            summary.artifacts += 1;
            summary.reserved_bytes = summary.reserved_bytes.saturating_add(transfer.byte_count);
            self.reserved_bytes = self.reserved_bytes.saturating_sub(transfer.byte_count);
            let binding = transfer.binding.clone();
            let path = transfer.path.clone();
            drop(transfer);
            let _ = fs::remove_file(path);
            if remember {
                self.remember(binding, now_ms);
            }
        }
        summary
    }

    fn abort_id(&mut self, artifact_id: &str, now_ms: u64, remember: bool) {
        self.cleanup_ids(vec![artifact_id.to_owned()], now_ms, remember);
    }

    fn remember(&mut self, binding: ArtifactBinding, now_ms: u64) {
        let artifact_id = binding.artifact_id().to_owned();
        if !self.tombstones.contains_key(&artifact_id) {
            self.tombstone_order.push_back(artifact_id.clone());
        }
        self.tombstones.insert(
            artifact_id,
            Tombstone {
                binding,
                expires_at_ms: now_ms.saturating_add(self.limits.tombstone_ttl_ms),
            },
        );
        self.prune_tombstones(now_ms);
        while self.tombstone_order.len() > self.limits.max_tombstones {
            if let Some(oldest) = self.tombstone_order.pop_front() {
                self.tombstones.remove(&oldest);
            }
        }
    }

    fn prune_tombstones(&mut self, now_ms: u64) {
        loop {
            let Some(oldest) = self.tombstone_order.front() else {
                return;
            };
            if self
                .tombstones
                .get(oldest)
                .is_some_and(|tombstone| tombstone.expires_at_ms > now_ms)
            {
                return;
            }
            let oldest = self.tombstone_order.pop_front().expect("front exists");
            self.tombstones.remove(&oldest);
        }
    }
}

impl Drop for ArtifactSpool {
    fn drop(&mut self) {
        self.close();
    }
}

fn progress(transfer: &Transfer, status: &'static str) -> ArtifactProgress {
    ArtifactProgress {
        artifact_id: transfer.binding.artifact_id().to_owned(),
        status,
        next_chunk_index: transfer.next_chunk_index,
        received_bytes: transfer.received_bytes,
    }
}

fn validate_identifier(value: &str) -> Result<(), ArtifactError> {
    if value.len() > MAX_IDENTIFIER_BYTES || !rpc::valid_opaque_id(value) {
        return Err(ArtifactError::InvalidBinding);
    }
    Ok(())
}

fn validate_mime_type(value: &str) -> Result<(), ArtifactError> {
    if value.is_empty()
        || value.len() > MAX_MIME_TYPE_BYTES
        || !value.is_ascii()
        || value.split_once('/').is_none_or(|(kind, subtype)| {
            kind.is_empty()
                || subtype.is_empty()
                || kind.bytes().any(|byte| !mime_byte(byte))
                || subtype.bytes().any(|byte| !mime_byte(byte))
        })
    {
        return Err(ArtifactError::ArtifactMimeInvalid);
    }
    Ok(())
}

fn mime_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
        || matches!(
            byte,
            b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
        )
}

fn parse_sha256(value: &str) -> Result<[u8; 32], ArtifactError> {
    let digest = value
        .strip_prefix("sha256:")
        .filter(|digest| digest.len() == 64)
        .ok_or(ArtifactError::ArtifactDigestInvalid)?;
    let mut decoded = [0_u8; 32];
    for (index, pair) in digest.as_bytes().chunks_exact(2).enumerate() {
        decoded[index] = (hex_nibble(pair[0]).ok_or(ArtifactError::ArtifactDigestInvalid)? << 4)
            | hex_nibble(pair[1]).ok_or(ArtifactError::ArtifactDigestInvalid)?;
    }
    Ok(decoded)
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        _ => None,
    }
}

#[cfg(windows)]
fn private_spool_parent(requested: Option<&Path>) -> Result<PathBuf, ArtifactError> {
    windows_private::parent(requested)
}

#[cfg(unix)]
fn private_spool_parent(requested: Option<&Path>) -> Result<PathBuf, ArtifactError> {
    use std::os::unix::fs::PermissionsExt;

    let requested = match requested {
        Some(path) => path.to_owned(),
        None if cfg!(target_os = "macos") => std::env::var_os("TMPDIR")
            .map(PathBuf::from)
            .ok_or(ArtifactError::PrivateSpoolUnavailable)?,
        None => std::env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .ok_or(ArtifactError::PrivateSpoolUnavailable)?,
    };
    if !requested.is_absolute() {
        return Err(ArtifactError::PrivateSpoolUnavailable);
    }
    let metadata =
        fs::symlink_metadata(&requested).map_err(|_| ArtifactError::PrivateSpoolUnavailable)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(ArtifactError::PrivateSpoolUnavailable);
    }
    let canonical = requested
        .canonicalize()
        .map_err(|_| ArtifactError::PrivateSpoolUnavailable)?;
    let canonical_metadata =
        fs::symlink_metadata(&canonical).map_err(|_| ArtifactError::PrivateSpoolUnavailable)?;
    if canonical_metadata.file_type().is_symlink()
        || !canonical_metadata.is_dir()
        || canonical_metadata.permissions().mode() & 0o077 != 0
    {
        return Err(ArtifactError::PrivateSpoolUnavailable);
    }
    Ok(canonical)
}

#[cfg(not(any(unix, windows)))]
fn private_spool_parent(_requested: Option<&Path>) -> Result<PathBuf, ArtifactError> {
    Err(ArtifactError::PrivateSpoolUnavailable)
}

#[cfg(unix)]
fn create_private_root(parent: &Path) -> Result<PathBuf, ArtifactError> {
    use std::os::unix::fs::PermissionsExt;

    for _ in 0..CREATE_ATTEMPTS {
        let path = parent.join(random_name(".a0-browser-artifacts-")?);
        match fs::create_dir(&path) {
            Ok(()) => {
                if fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).is_err() {
                    let _ = fs::remove_dir(&path);
                    return Err(ArtifactError::PrivateSpoolUnavailable);
                }
                return Ok(path);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(ArtifactError::PrivateSpoolUnavailable),
        }
    }
    Err(ArtifactError::PrivateSpoolUnavailable)
}

#[cfg(windows)]
fn create_private_root(parent: &Path) -> Result<PathBuf, ArtifactError> {
    windows_private::create_root(parent)
}

#[cfg(not(any(unix, windows)))]
fn create_private_root(_parent: &Path) -> Result<PathBuf, ArtifactError> {
    Err(ArtifactError::PrivateSpoolUnavailable)
}

#[cfg(unix)]
fn create_private_file(root: &Path, suffix: &str) -> Result<(PathBuf, File), ArtifactError> {
    use std::os::unix::fs::PermissionsExt;

    for _ in 0..CREATE_ATTEMPTS {
        let path = root.join(format!("{}{suffix}", random_name("attachment-")?));
        match File::options()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => {
                if fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).is_err() {
                    drop(file);
                    let _ = fs::remove_file(&path);
                    return Err(ArtifactError::SpoolIo);
                }
                return Ok((path, file));
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(ArtifactError::SpoolIo),
        }
    }
    Err(ArtifactError::SpoolIo)
}

#[cfg(windows)]
fn create_private_file(root: &Path, suffix: &str) -> Result<(PathBuf, File), ArtifactError> {
    windows_private::create_file(root, suffix)
}

#[cfg(not(any(unix, windows)))]
fn create_private_file(_root: &Path, _suffix: &str) -> Result<(PathBuf, File), ArtifactError> {
    Err(ArtifactError::PrivateSpoolUnavailable)
}

fn input_mime_suffix(mime_type: &str) -> &'static str {
    // This is a native-chosen filename, not any caller-provided path/name.
    // Chrome derives File.type from the suffix when setting a private file.
    match mime_type {
        "text/plain" => ".txt",
        "text/csv" => ".csv",
        "text/html" => ".html",
        "application/pdf" => ".pdf",
        "application/json" => ".json",
        "application/zip" => ".zip",
        "image/png" => ".png",
        "image/jpeg" => ".jpg",
        "image/gif" => ".gif",
        "image/webp" => ".webp",
        "image/svg+xml" => ".svg",
        "audio/mpeg" => ".mp3",
        "audio/wav" => ".wav",
        "video/mp4" => ".mp4",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" => ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" => ".xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation" => ".pptx",
        _ => ".bin",
    }
}

fn random_name(prefix: &str) -> Result<String, ArtifactError> {
    let mut random = [0_u8; RANDOM_NAME_BYTES];
    getrandom::fill(&mut random).map_err(|_| ArtifactError::PrivateSpoolUnavailable)?;
    let mut output = String::with_capacity(prefix.len() + RANDOM_NAME_BYTES * 2);
    output.push_str(prefix);
    for byte in random {
        use std::fmt::Write as _;
        write!(&mut output, "{byte:02x}").expect("string formatting is infallible");
    }
    Ok(output)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::Read;
    use std::os::unix::fs::PermissionsExt;

    const ORIGIN: &str = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/";

    fn private_parent() -> PathBuf {
        let parent = std::env::temp_dir().join(random_name("a0-artifact-test-").unwrap());
        fs::create_dir(&parent).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
        parent
    }

    fn route() -> ArtifactRoute {
        let invocation = NativeInvocation::fixture(ORIGIN);
        ArtifactRoute::from_validated_invocation(
            &invocation,
            "install-1",
            "load-1",
            "server-1",
            "bridge-1",
            3,
            "sid-1",
        )
        .unwrap()
    }

    fn binding(route: &ArtifactRoute, artifact_id: &str) -> ArtifactBinding {
        ArtifactBinding::new(
            route,
            "context-1",
            "session-1",
            "turn-1",
            "action-1",
            "op-1",
            artifact_id,
            ArtifactDirection::Output,
            ArtifactPurpose::Screenshot,
        )
        .unwrap()
    }

    fn digest(bytes: &[u8]) -> String {
        let result = Sha256::digest(bytes);
        let mut output = String::from("sha256:");
        for byte in result {
            use std::fmt::Write as _;
            write!(&mut output, "{byte:02x}").unwrap();
        }
        output
    }

    fn spool(
        parent: &Path,
        authorizer: CurrentRouteAuthorizer,
        limits: ArtifactLimits,
    ) -> ArtifactSpool {
        ArtifactSpool::new(Some(parent), authorizer, 0, limits).unwrap()
    }

    #[test]
    fn verified_private_spool_is_pathless_and_single_consume() {
        let parent = private_parent();
        let route = route();
        let primary = binding(&route, "artifact-1");
        let payload = b"verified screenshot";
        let mut spool = spool(&parent, Arc::new(|_| true), ArtifactLimits::default());
        assert_eq!(
            fs::metadata(&spool.root).unwrap().permissions().mode() & 0o777,
            0o700
        );
        spool
            .begin(
                &primary,
                payload.len() as u64,
                &digest(payload),
                "image/png",
                1,
            )
            .unwrap();
        let path = spool
            .transfers
            .get(primary.artifact_id())
            .unwrap()
            .path
            .clone();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        spool.append(&primary, 0, &payload[..8], 2).unwrap();
        spool.append(&primary, 1, &payload[8..], 3).unwrap();
        let descriptor = spool.complete(&primary, 4).unwrap();
        assert_eq!(descriptor.artifact_id(), "artifact-1");
        assert_eq!(descriptor.byte_count(), payload.len() as u64);
        assert_eq!(descriptor.sha256(), digest(payload));
        let consumed = spool
            .consume(&primary, 5, |reader, received| {
                assert_eq!(received, &descriptor);
                let mut bytes = Vec::new();
                reader
                    .read_to_end(&mut bytes)
                    .map_err(|_| ArtifactError::SpoolIo)?;
                Ok(bytes)
            })
            .unwrap();
        assert_eq!(consumed, payload);
        assert_eq!(spool.active_count(), 0);
        assert_eq!(spool.reserved_bytes(), 0);
        assert!(!path.exists());
        assert_eq!(
            spool.consume(&primary, 6, |_reader, _descriptor| Ok(())),
            Err(ArtifactError::ArtifactTerminal)
        );

        let panicking = binding(&route, "artifact-panic");
        spool
            .begin(&panicking, 1, &digest(b"x"), "image/png", 7)
            .unwrap();
        spool.append(&panicking, 0, b"x", 8).unwrap();
        spool.complete(&panicking, 9).unwrap();
        let unwind = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _: Result<(), ArtifactError> =
                spool.consume(&panicking, 10, |_reader, _descriptor| panic!("consumer"));
        }));
        assert!(unwind.is_err());
        assert_eq!(spool.active_count(), 0);
        assert_eq!(spool.reserved_bytes(), 0);
        spool.close();
        assert!(!spool.root.exists());
        fs::remove_dir(parent).unwrap();
    }

    #[test]
    fn every_binding_dimension_is_exact_and_cross_binding_does_not_abort_owner() {
        let parent = private_parent();
        let route = route();
        let owner = binding(&route, "artifact-1");
        let mut spool = spool(&parent, Arc::new(|_| true), ArtifactLimits::default());
        spool
            .begin(&owner, 3, &digest(b"abc"), "image/png", 1)
            .unwrap();
        let mut routes = Vec::new();
        for (install, load, server, bridge, generation, sid) in [
            ("other", "load-1", "server-1", "bridge-1", 3, "sid-1"),
            ("install-1", "other", "server-1", "bridge-1", 3, "sid-1"),
            ("install-1", "load-1", "other", "bridge-1", 3, "sid-1"),
            ("install-1", "load-1", "server-1", "other", 3, "sid-1"),
            ("install-1", "load-1", "server-1", "bridge-1", 4, "sid-1"),
            ("install-1", "load-1", "server-1", "bridge-1", 3, "other"),
        ] {
            let invocation = NativeInvocation::fixture(ORIGIN);
            routes.push(
                ArtifactRoute::from_validated_invocation(
                    &invocation,
                    install,
                    load,
                    server,
                    bridge,
                    generation,
                    sid,
                )
                .unwrap(),
            );
        }
        let other_invocation =
            NativeInvocation::fixture("chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/");
        routes.push(
            ArtifactRoute::from_validated_invocation(
                &other_invocation,
                "install-1",
                "load-1",
                "server-1",
                "bridge-1",
                3,
                "sid-1",
            )
            .unwrap(),
        );
        let mut probes = routes
            .iter()
            .map(|route| binding(route, "artifact-1"))
            .collect::<Vec<_>>();
        for (context, session, turn, action, op) in [
            ("other", "session-1", "turn-1", "action-1", "op-1"),
            ("context-1", "other", "turn-1", "action-1", "op-1"),
            ("context-1", "session-1", "other", "action-1", "op-1"),
            ("context-1", "session-1", "turn-1", "other", "op-1"),
            ("context-1", "session-1", "turn-1", "action-1", "other"),
        ] {
            probes.push(
                ArtifactBinding::new(
                    &route,
                    context,
                    session,
                    turn,
                    action,
                    op,
                    "artifact-1",
                    ArtifactDirection::Output,
                    ArtifactPurpose::Screenshot,
                )
                .unwrap(),
            );
        }
        probes.push(
            ArtifactBinding::new(
                &route,
                "context-1",
                "session-1",
                "turn-1",
                "action-1",
                "op-1",
                "artifact-1",
                ArtifactDirection::Input,
                ArtifactPurpose::UploadFile,
            )
            .unwrap(),
        );
        for probe in probes {
            assert_eq!(
                spool.append(&probe, 0, b"abc", 2),
                Err(ArtifactError::ScopeDenied)
            );
            assert_eq!(spool.active_count(), 1);
        }
        spool.append(&owner, 0, b"abc", 2).unwrap();
        spool.complete(&owner, 3).unwrap();
        spool.close();
        fs::remove_dir(parent).unwrap();
    }

    #[test]
    fn ordering_chunk_declaration_and_integrity_failures_abort() {
        let parent = private_parent();
        let route = route();
        let mut spool = spool(&parent, Arc::new(|_| true), ArtifactLimits::default());
        for (offset, (id, failure)) in [
            ("order", ArtifactError::ChunkOutOfOrder),
            ("chunk", ArtifactError::ChunkSizeInvalid),
            ("overrun", ArtifactError::ArtifactSizeMismatch),
        ]
        .into_iter()
        .enumerate()
        {
            let begin_at = 1 + u64::try_from(offset).unwrap() * 2;
            let binding = binding(&route, id);
            spool
                .begin(&binding, 3, &digest(b"abc"), "image/png", begin_at)
                .unwrap();
            let result = match id {
                "order" => spool.append(&binding, 1, b"abc", begin_at + 1),
                "chunk" => spool.append(&binding, 0, &vec![0; MAX_CHUNK_BYTES + 1], begin_at + 1),
                _ => spool.append(&binding, 0, b"abcd", begin_at + 1),
            };
            assert_eq!(result, Err(failure));
            assert_eq!(spool.active_count(), 0);
        }
        let corrupt = binding(&route, "corrupt");
        spool
            .begin(&corrupt, 3, &digest(b"xyz"), "image/png", 7)
            .unwrap();
        spool.append(&corrupt, 0, b"abc", 8).unwrap();
        assert_eq!(
            spool.complete(&corrupt, 9),
            Err(ArtifactError::ArtifactIntegrityMismatch)
        );
        assert_eq!(spool.active_count(), 0);

        let truncated = binding(&route, "truncated");
        spool
            .begin(&truncated, 3, &digest(b"abc"), "image/png", 10)
            .unwrap();
        spool.append(&truncated, 0, b"abc", 11).unwrap();
        spool
            .transfers
            .get(&truncated.artifact_id)
            .unwrap()
            .file
            .set_len(2)
            .unwrap();
        assert_eq!(
            spool.complete(&truncated, 12),
            Err(ArtifactError::ArtifactIntegrityMismatch)
        );
        assert_eq!(spool.active_count(), 0);
        assert_eq!(
            spool.begin(
                &binding(&route, "oversized"),
                MAX_ARTIFACT_BYTES + 1,
                &digest(b""),
                "image/png",
                13,
            ),
            Err(ArtifactError::ArtifactSizeInvalid)
        );
        spool.close();
        fs::remove_dir(parent).unwrap();
    }

    #[test]
    fn authority_is_mandatory_rechecked_and_loss_purges() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

        let parent = private_parent();
        let route = route();
        let binding = binding(&route, "artifact-1");
        let calls = Arc::new(AtomicUsize::new(0));
        let authority_calls = Arc::clone(&calls);
        let mut spool = spool(
            &parent,
            Arc::new(move |_| authority_calls.fetch_add(1, Ordering::AcqRel) < 4),
            ArtifactLimits::default(),
        );
        spool
            .begin(&binding, 3, &digest(b"abc"), "image/png", 1)
            .unwrap();
        spool.append(&binding, 0, b"abc", 2).unwrap();
        spool.complete(&binding, 3).unwrap();
        let called = Arc::new(AtomicBool::new(false));
        let observed = Arc::clone(&called);
        assert_eq!(
            spool.consume(&binding, 4, move |_reader, _descriptor| {
                observed.store(true, Ordering::Release);
                Ok(())
            }),
            Err(ArtifactError::ScopeDeniedAndAborted)
        );
        assert!(!called.load(Ordering::Acquire));
        assert_eq!(calls.load(Ordering::Acquire), 5);
        assert_eq!(spool.active_count(), 0);
        assert_eq!(spool.reserved_bytes(), 0);
        spool.close();
        fs::remove_dir(parent).unwrap();
    }

    #[test]
    fn capacity_is_retained_through_consumer_and_cleanup_is_exact() {
        let parent = private_parent();
        let route = route();
        let primary = binding(&route, "artifact-1");
        let limits = ArtifactLimits {
            max_artifacts: 1,
            max_total_spool_bytes: 3,
            ..ArtifactLimits::default()
        };
        let mut spool = spool(&parent, Arc::new(|_| true), limits);
        spool
            .begin(&primary, 3, &digest(b"abc"), "image/png", 1)
            .unwrap();
        assert_eq!(
            spool.begin(
                &binding(&route, "capacity"),
                1,
                &digest(b"x"),
                "image/png",
                1,
            ),
            Err(ArtifactError::RegistryFull)
        );
        spool.append(&primary, 0, b"abc", 2).unwrap();
        spool.complete(&primary, 3).unwrap();
        let seen = spool
            .consume(&primary, 4, |reader, _descriptor| {
                let mut bytes = Vec::new();
                reader
                    .read_to_end(&mut bytes)
                    .map_err(|_| ArtifactError::SpoolIo)?;
                Ok(bytes)
            })
            .unwrap();
        assert_eq!(seen, b"abc");
        assert_eq!(spool.reserved_bytes(), 0);

        let expiring = binding(&route, "expiring");
        spool
            .begin(&expiring, 3, &digest(b"abc"), "image/png", 5)
            .unwrap();
        assert_eq!(spool.expire(5 + limits.ttl_ms).unwrap().artifacts, 1);
        let disconnecting = binding(&route, "disconnecting");
        spool
            .begin(
                &disconnecting,
                3,
                &digest(b"abc"),
                "image/png",
                6 + limits.ttl_ms,
            )
            .unwrap();
        assert_eq!(
            spool
                .disconnect(&route, 7 + limits.ttl_ms)
                .unwrap()
                .artifacts,
            1
        );
        let revoking = binding(&route, "revoking");
        spool
            .begin(
                &revoking,
                3,
                &digest(b"abc"),
                "image/png",
                8 + limits.ttl_ms,
            )
            .unwrap();
        assert_eq!(
            spool
                .revoke_bridge("bridge-1", 2, 9 + limits.ttl_ms)
                .unwrap()
                .artifacts,
            0
        );
        assert_eq!(
            spool
                .revoke_bridge("bridge-1", 3, 10 + limits.ttl_ms)
                .unwrap()
                .artifacts,
            1
        );
        spool.close();
        fs::remove_dir(parent).unwrap();
    }

    #[test]
    fn input_purpose_and_private_parent_are_fail_closed() {
        let parent = private_parent();
        let route = route();
        assert_eq!(
            ArtifactBinding::new(
                &route,
                "context-1",
                "session-1",
                "turn-1",
                "action-1",
                "op-1",
                "artifact-1",
                ArtifactDirection::Input,
                ArtifactPurpose::Screenshot,
            ),
            Err(ArtifactError::InvalidPurpose)
        );
        let input = ArtifactBinding::new(
            &route,
            "context-1",
            "session-1",
            "turn-1",
            "action-1",
            "op-1",
            "input-1",
            ArtifactDirection::Input,
            ArtifactPurpose::UploadFile,
        )
        .unwrap();
        assert_eq!(input.direction().as_str(), "input");
        assert_eq!(input.purpose().as_str(), "upload_file");

        let limits = ArtifactLimits {
            max_artifacts: 2,
            max_total_spool_bytes: 3,
            ..ArtifactLimits::default()
        };
        let mut bounded = spool(&parent, Arc::new(|_| true), limits);
        let first = binding(&route, "first");
        bounded
            .begin(&first, 2, &digest(b"ab"), "image/png", 1)
            .unwrap();
        assert_eq!(
            bounded.begin(
                &binding(&route, "second"),
                2,
                &digest(b"cd"),
                "image/png",
                2,
            ),
            Err(ArtifactError::SpoolFull)
        );
        bounded.close();

        let shared = std::env::temp_dir().join(random_name("a0-artifact-shared-").unwrap());
        fs::create_dir(&shared).unwrap();
        fs::set_permissions(&shared, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(matches!(
            ArtifactSpool::new(
                Some(&shared),
                Arc::new(|_| true),
                0,
                ArtifactLimits::default()
            ),
            Err(ArtifactError::PrivateSpoolUnavailable)
        ));
        fs::remove_dir(shared).unwrap();
        let symlink = std::env::temp_dir().join(random_name("a0-artifact-link-").unwrap());
        std::os::unix::fs::symlink(&parent, &symlink).unwrap();
        assert!(matches!(
            ArtifactSpool::new(
                Some(&symlink),
                Arc::new(|_| true),
                0,
                ArtifactLimits::default()
            ),
            Err(ArtifactError::PrivateSpoolUnavailable)
        ));
        fs::remove_file(symlink).unwrap();
        fs::remove_dir(parent).unwrap();
    }
}
