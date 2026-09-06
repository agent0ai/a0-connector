//! Host-owned Browser bridge pairing backend.
//!
//! The companion generates the Ed25519 key locally, posts only the public key
//! to Agent Zero, and commits the private seed only to the platform credential
//! store after an exact successful exchange response. No plaintext file
//! fallback exists until the extension can carry the required explicit user
//! acknowledgement.

#![cfg_attr(test, allow(dead_code))]

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::time::Duration;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use ed25519_dalek::SigningKey;
use sha2::{Digest, Sha256};
#[cfg(not(test))]
use zeroize::Zeroize;
use zeroize::Zeroizing;

use crate::json::{self, Value};
#[cfg(feature = "local-development")]
use crate::rpc::valid_development_server_base_origin;
#[cfg(not(feature = "local-development"))]
use crate::rpc::valid_server_base_origin;
use crate::rpc::{is_extension_id, valid_opaque_id, valid_pairing_code, CONTRACT_VERSION};
use crate::COMPANION_VERSION;

#[cfg(not(feature = "local-development"))]
const TRUST_CONTRACT: &str = "a0.browser-bridge.trust.v1";
#[cfg(feature = "local-development")]
const TRUST_CONTRACT: &str = crate::DEVELOPMENT_TRUST_CONTRACT;
#[cfg(not(feature = "local-development"))]
const CORE_PAIRING_PATH: &str = "/api/plugins/_a0_connector/browser_bridge_exchange";
#[cfg(feature = "local-development")]
const CORE_PAIRING_PATH: &str = "/api/plugins/_a0_connector/browser_bridge_development_exchange";
#[cfg(not(feature = "local-development"))]
const CORE_CHALLENGE_PATH: &str = "/api/plugins/_a0_connector/browser_bridge_challenge";
#[cfg(feature = "local-development")]
const CORE_CHALLENGE_PATH: &str = crate::development_session::DEVELOPMENT_CHALLENGE_PATH;
const CONNECTOR_PROTOCOL: &str = "a0-connector.v1";
const BROWSER_PROTOCOL: &str = "a0.browser-bridge.v1";
const ADAPTER_CONTRACT: &str = "a0.browser-bridge.adapter.v1";
const MV3_RUNTIME_CONTRACT: &str = "a0.browser-bridge.mv3-runtime.v1";
#[cfg(not(feature = "local-development"))]
const CREDENTIAL_SERVICE: &str = "io.agentzero.browser_bridge";
#[cfg(feature = "local-development")]
const CREDENTIAL_SERVICE: &str = "io.agentzero.browser_bridge.dev";
#[cfg(not(feature = "local-development"))]
const INSTALLATION_ENTRY: &str = "installation-v1";
#[cfg(feature = "local-development")]
const INSTALLATION_ENTRY: &str = "development-installation-v1";
#[cfg(not(feature = "local-development"))]
const LEGACY_CREDENTIAL_ENTRY: &str = "active-credential-v1";
#[cfg(feature = "local-development")]
const LEGACY_CREDENTIAL_ENTRY: &str = "development-active-credential-v1";
#[cfg(not(feature = "local-development"))]
const PROFILE_CREDENTIAL_PREFIX: &str = "profile-credential-v2-";
#[cfg(feature = "local-development")]
const PROFILE_CREDENTIAL_PREFIX: &str = "development-profile-credential-v2-";
const CREDENTIAL_MAGIC: &[u8; 8] = b"A0BBCRED";
const LEGACY_CREDENTIAL_FORMAT_VERSION: u8 = 1;
const CREDENTIAL_FORMAT_VERSION: u8 = 2;
const PRIVATE_KEY_BYTES: usize = 32;
const INSTALLATION_ID_BYTES: usize = 16;
const MAX_CORE_RESPONSE_BYTES: usize = 16 * 1024;
const MAX_KEY_GENERATION: u32 = 2_147_483_647;
const PENDING_ROTATION_MAGIC: &[u8; 8] = b"A0BBROT1";
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);

const FIXED_SCOPES: &[&str] = &[
    "bridge.connect",
    "context.list",
    "context.read",
    "context.message",
    "browser.operate",
    "browser.control",
    "browser.artifact",
    "browser.approval",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PairingFailure {
    #[cfg(test)]
    BackendUnavailable,
    CredentialStoreUnavailable,
    CredentialCorrupt,
    LegacyCredentialPresent,
    AlreadyPaired,
    ExtensionBindingMismatch,
    ProfileBindingMismatch,
    EntropyUnavailable,
    ExchangeRejected,
    ExchangeOutcomeUnknown,
    ExchangeResponseInvalid,
    CredentialCommitFailed,
}

impl PairingFailure {
    pub(crate) const fn code(self) -> &'static str {
        match self {
            #[cfg(test)]
            Self::BackendUnavailable => "PAIRING_BACKEND_UNAVAILABLE",
            Self::CredentialStoreUnavailable => "CREDENTIAL_STORE_UNAVAILABLE",
            Self::CredentialCorrupt => "PAIRING_REPAIR_REQUIRED",
            Self::LegacyCredentialPresent => "PAIRING_LEGACY_RECORD_PRESENT",
            Self::AlreadyPaired => "PAIRING_ALREADY_PAIRED",
            Self::ExtensionBindingMismatch => "PAIRING_EXTENSION_MISMATCH",
            Self::ProfileBindingMismatch => "PAIRING_PROFILE_MISMATCH",
            Self::EntropyUnavailable => "SECURE_ENTROPY_UNAVAILABLE",
            Self::ExchangeRejected => "PAIRING_EXCHANGE_FAILED",
            Self::ExchangeOutcomeUnknown => "PAIRING_EXCHANGE_OUTCOME_UNKNOWN",
            Self::ExchangeResponseInvalid => "PAIRING_EXCHANGE_RESPONSE_INVALID",
            Self::CredentialCommitFailed => "PAIRING_CREDENTIAL_COMMIT_FAILED",
        }
    }

    pub(crate) const fn message(self) -> &'static str {
        match self {
            #[cfg(test)]
            Self::BackendUnavailable => {
                "This companion build cannot exchange pairing credentials."
            }
            Self::CredentialStoreUnavailable => {
                "The operating-system credential store is unavailable."
            }
            Self::CredentialCorrupt => "The stored Browser bridge credential needs repair.",
            Self::LegacyCredentialPresent => {
                "A legacy Browser bridge credential is present; remove it and pair this Chrome profile again."
            }
            Self::AlreadyPaired => "Disconnect the existing Browser bridge before pairing again.",
            Self::ExtensionBindingMismatch => {
                "The stored Browser bridge credential belongs to another extension identity."
            }
            Self::ProfileBindingMismatch => {
                "The stored Browser bridge credential belongs to another Chrome profile."
            }
            Self::EntropyUnavailable => "Secure operating-system entropy is unavailable.",
            Self::ExchangeRejected => "Agent Zero rejected the pairing exchange.",
            Self::ExchangeOutcomeUnknown => {
                "The pairing exchange outcome is unknown; create a new code and revoke any orphaned record."
            }
            Self::ExchangeResponseInvalid => {
                "Agent Zero returned an invalid pairing response; revoke the server record before retrying."
            }
            Self::CredentialCommitFailed => {
                "Pairing reached Agent Zero but the local credential could not be stored; revoke the server record."
            }
        }
    }

    pub(crate) const fn outcome(self) -> &'static str {
        match self {
            #[cfg(test)]
            Self::BackendUnavailable => "not_applied",
            Self::ExchangeOutcomeUnknown
            | Self::ExchangeResponseInvalid
            | Self::CredentialCommitFailed => "unknown",
            _ => "not_applied",
        }
    }

    pub(crate) const fn retryable(self) -> bool {
        false
    }
}

#[derive(Clone, Debug)]
pub(crate) struct PairingHello {
    pub companion_instance_id: String,
    pub server_state: &'static str,
    pub server_instance_id: Option<String>,
}

pub(crate) struct CredentialRecord {
    pub(crate) bridge_id: String,
    pub(crate) server_instance_id: String,
    pub(crate) server_base_origin: String,
    pub(crate) extension_id: String,
    pub(crate) install_instance_id: String,
    companion_instance_id: String,
    key_generation: u32,
    created_at_ms: u64,
    pub(crate) private_seed: Zeroizing<[u8; PRIVATE_KEY_BYTES]>,
}

/// Stored only in the profile's OS credential-store slot; never serialized to Chrome.
pub(crate) struct PendingRotation {
    pub(crate) rotation_id: String,
    pub(crate) credential: CredentialRecord,
}

impl CredentialRecord {
    pub(crate) fn companion_instance_id(&self) -> &str {
        &self.companion_instance_id
    }

    pub(crate) const fn key_generation(&self) -> u32 {
        self.key_generation
    }
}

#[cfg(test)]
impl CredentialRecord {
    pub(crate) fn fixture(base: &str) -> Self {
        Self {
            bridge_id: "bridge-fixture".into(),
            server_instance_id: "server-fixture".into(),
            server_base_origin: base.into(),
            extension_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            install_instance_id: "install-fixture".into(),
            companion_instance_id: "companion-fixture".into(),
            key_generation: 1,
            created_at_ms: 1,
            private_seed: Zeroizing::new([7; 32]),
        }
    }

    #[cfg(feature = "local-development")]
    pub(crate) fn fixture_development() -> Self {
        Self {
            bridge_id: "bridge-dev-fixture".into(),
            server_instance_id: "server-dev-fixture".into(),
            server_base_origin: "http://localhost:50080".into(),
            extension_id: crate::DEVELOPMENT_EXTENSION_ID.into(),
            install_instance_id: "install-dev-fixture".into(),
            companion_instance_id: "companion-dev-fixture".into(),
            key_generation: 1,
            created_at_ms: 1,
            private_seed: Zeroizing::new([
                0x9d, 0x61, 0xb1, 0x9d, 0xef, 0xfd, 0x5a, 0x60, 0xba, 0x84, 0x4a, 0xf4, 0x92, 0xec,
                0x2c, 0xc4, 0x44, 0x49, 0xc5, 0x69, 0x7b, 0x32, 0x69, 0x19, 0x70, 0x3b, 0xac, 0x03,
                0x1c, 0xae, 0x7f, 0x60,
            ]),
        }
    }
}

trait CredentialStore: Send + Sync {
    fn load_installation_id(&self) -> Result<Option<[u8; INSTALLATION_ID_BYTES]>, PairingFailure>;
    fn save_installation_id(
        &self,
        installation_id: &[u8; INSTALLATION_ID_BYTES],
    ) -> Result<(), PairingFailure>;
    fn load_credential(
        &self,
        extension_id: &str,
        install_instance_id: &str,
    ) -> Result<Option<CredentialRecord>, PairingFailure>;
    fn save_credential(&self, credential: &CredentialRecord) -> Result<(), PairingFailure>;
    fn delete_credential(
        &self,
        extension_id: &str,
        install_instance_id: &str,
    ) -> Result<(), PairingFailure>;
    fn load_rotation(
        &self,
        _extension: &str,
        _install: &str,
    ) -> Result<Option<Zeroizing<Vec<u8>>>, PairingFailure> {
        Err(PairingFailure::CredentialStoreUnavailable)
    }
    fn save_rotation(
        &self,
        _extension: &str,
        _install: &str,
        _bytes: &[u8],
    ) -> Result<(), PairingFailure> {
        Err(PairingFailure::CredentialStoreUnavailable)
    }
    fn delete_rotation(&self, _extension: &str, _install: &str) -> Result<(), PairingFailure> {
        Ok(())
    }
}

trait PairingTransport: Send + Sync {
    fn exchange(&self, endpoint: &str, body: &[u8]) -> Result<Vec<u8>, PairingFailure>;
}

trait EntropySource: Send + Sync {
    fn fill(&self, destination: &mut [u8]) -> Result<(), PairingFailure>;
}

pub(crate) struct PairingService {
    credential_store: Box<dyn CredentialStore>,
    transport: Box<dyn PairingTransport>,
    entropy: Box<dyn EntropySource>,
}

impl PairingService {
    pub(crate) fn pending_rotation(
        &self,
        extension: &str,
        install: &str,
    ) -> Result<Option<PendingRotation>, PairingFailure> {
        let active = self
            .connector_credential(extension, install)?
            .ok_or(PairingFailure::CredentialCorrupt)?;
        let Some(bytes) = self.credential_store.load_rotation(extension, install)? else {
            return Ok(None);
        };
        let pending = decode_rotation(&bytes)?;
        validate_record_binding(&pending.credential, extension, install)?;
        if pending.credential.bridge_id != active.bridge_id
            || pending.credential.server_instance_id != active.server_instance_id
            || pending.credential.server_base_origin != active.server_base_origin
            || pending.credential.companion_instance_id != active.companion_instance_id
            || !(pending.credential.key_generation == active.key_generation.saturating_add(1)
                || (pending.credential.key_generation == active.key_generation
                    && pending.credential.private_seed[..] == active.private_seed[..]))
        {
            return Err(PairingFailure::CredentialCorrupt);
        }
        Ok(Some(pending))
    }

    /// Reserve the private candidate durably before sending its public key to Core.
    /// An interrupted attempt reuses the exact rotation id and key, never replaces it.
    pub(crate) fn stage_rotation(
        &self,
        extension: &str,
        install: &str,
    ) -> Result<Value, PairingFailure> {
        let _lock = CredentialMutationLock::acquire(extension, install)?;
        let pending = match self.pending_rotation(extension, install)? {
            Some(pending) => pending,
            None => {
                let mut credential = self
                    .connector_credential(extension, install)?
                    .ok_or(PairingFailure::CredentialCorrupt)?;
                credential.key_generation = credential
                    .key_generation
                    .checked_add(1)
                    .filter(|generation| *generation <= MAX_KEY_GENERATION)
                    .ok_or(PairingFailure::CredentialCorrupt)?;
                self.entropy.fill(&mut credential.private_seed[..])?;
                let mut rotation_bytes = [0_u8; 16];
                self.entropy.fill(&mut rotation_bytes)?;
                let pending = PendingRotation {
                    rotation_id: format_uuid(rotation_bytes),
                    credential,
                };
                self.credential_store.save_rotation(
                    extension,
                    install,
                    &encode_rotation(&pending)?,
                )?;
                pending
            }
        };
        let public = SigningKey::from_bytes(&pending.credential.private_seed).verifying_key();
        Ok(Value::Object(BTreeMap::from([
            ("contract_version".into(), Value::Number("1".into())),
            ("action".into(), Value::String("rotate".into())),
            ("rotation_id".into(), Value::String(pending.rotation_id)),
            (
                "public_key".into(),
                Value::Object(BTreeMap::from([
                    ("algorithm".into(), Value::String("Ed25519".into())),
                    ("encoding".into(), Value::String("raw-base64url".into())),
                    (
                        "value".into(),
                        Value::String(URL_SAFE_NO_PAD.encode(public.as_bytes())),
                    ),
                ])),
            ),
        ])))
    }

    /// Only the Core connection worker may call this after authenticating with the
    /// candidate and checking Core's exact active rotation id and generation.
    pub(crate) fn commit_rotation_authenticated(
        &self,
        extension: &str,
        install: &str,
        rotation_id: &str,
        key_generation: u32,
    ) -> Result<(), PairingFailure> {
        let _lock = CredentialMutationLock::acquire(extension, install)?;
        let pending = self
            .pending_rotation(extension, install)?
            .ok_or(PairingFailure::CredentialCorrupt)?;
        if pending.rotation_id != rotation_id || pending.credential.key_generation != key_generation
        {
            return Err(PairingFailure::CredentialCorrupt);
        }
        // Save first. A crash between writes is recoverable: pending_rotation also
        // accepts the exact already-promoted key, but never a different same-gen key.
        self.credential_store.save_credential(&pending.credential)?;
        self.credential_store.delete_rotation(extension, install)
    }

    pub(crate) fn expire_rotation(
        &self,
        extension: &str,
        install: &str,
        rotation_id: &str,
    ) -> Result<(), PairingFailure> {
        let _lock = CredentialMutationLock::acquire(extension, install)?;
        let Some(pending) = self.pending_rotation(extension, install)? else {
            return Ok(());
        };
        if pending.rotation_id != rotation_id {
            return Err(PairingFailure::CredentialCorrupt);
        }
        self.credential_store.delete_rotation(extension, install)
    }

    /// A late revocation receipt must not delete a newer pairing in this profile.
    pub(crate) fn revoke_authenticated(
        &self,
        extension: &str,
        install: &str,
        bridge: &str,
        server: &str,
        generation: u32,
    ) -> Result<(), PairingFailure> {
        let _lock = CredentialMutationLock::acquire(extension, install)?;
        let active = self
            .connector_credential(extension, install)?
            .ok_or(PairingFailure::CredentialCorrupt)?;
        if active.bridge_id != bridge
            || active.server_instance_id != server
            || active.key_generation != generation
        {
            return Err(PairingFailure::CredentialCorrupt);
        }
        self.credential_store.delete_rotation(extension, install)?;
        self.credential_store.delete_credential(extension, install)
    }

    pub(crate) fn connector_credential(
        &self,
        caller_extension_id: &str,
        install_instance_id: &str,
    ) -> Result<Option<CredentialRecord>, PairingFailure> {
        validate_profile_identity(caller_extension_id, install_instance_id)
            .map_err(|_| PairingFailure::CredentialCorrupt)?;
        let record = self
            .credential_store
            .load_credential(caller_extension_id, install_instance_id)?;
        if let Some(value) = &record {
            validate_record_binding(value, caller_extension_id, install_instance_id)?;
        }
        Ok(record)
    }

    pub(crate) fn connector_challenge(
        &self,
        server_base: &str,
        body: &[u8],
    ) -> Result<Vec<u8>, PairingFailure> {
        if !valid_pairing_server_base_origin(server_base) {
            return Err(PairingFailure::CredentialCorrupt);
        }
        self.transport
            .exchange(&format!("{server_base}{CORE_CHALLENGE_PATH}"), body)
    }

    #[cfg(not(test))]
    pub(crate) fn system() -> Self {
        Self {
            credential_store: Box::new(SystemCredentialStore),
            transport: Box::new(SystemPairingTransport::new()),
            entropy: Box::new(SystemEntropy),
        }
    }

    #[cfg(test)]
    pub(crate) fn fixture_unavailable() -> Self {
        Self {
            credential_store: Box::new(EmptyCredentialStore),
            transport: Box::new(UnavailableTransport),
            entropy: Box::new(SystemEntropy),
        }
    }

    pub(crate) fn hello(
        &self,
        caller_extension_id: &str,
        install_instance_id: &str,
    ) -> PairingHello {
        if validate_profile_identity(caller_extension_id, install_instance_id).is_err() {
            return PairingHello {
                companion_instance_id: "unconfigured-companion".to_owned(),
                server_state: "repair_required",
                server_instance_id: None,
            };
        }
        match self
            .credential_store
            .load_credential(caller_extension_id, install_instance_id)
        {
            Ok(Some(record))
                if validate_record_binding(&record, caller_extension_id, install_instance_id)
                    .is_ok() =>
            {
                PairingHello {
                    companion_instance_id: record.companion_instance_id,
                    server_state: "paired",
                    server_instance_id: Some(record.server_instance_id),
                }
            }
            Ok(Some(_)) => PairingHello {
                companion_instance_id: "unconfigured-companion".to_owned(),
                server_state: "repair_required",
                server_instance_id: None,
            },
            Ok(None) => PairingHello {
                companion_instance_id: "unconfigured-companion".to_owned(),
                server_state: "unpaired",
                server_instance_id: None,
            },
            Err(_) => PairingHello {
                companion_instance_id: "unconfigured-companion".to_owned(),
                server_state: "repair_required",
                server_instance_id: None,
            },
        }
    }

    pub(crate) fn status(&self, caller_extension_id: &str, install_instance_id: &str) -> Value {
        if validate_profile_identity(caller_extension_id, install_instance_id).is_err() {
            return pairing_status_value(
                "repair_required",
                None,
                vec![diagnostic(
                    PairingFailure::ProfileBindingMismatch.code(),
                    PairingFailure::ProfileBindingMismatch.message(),
                    Some("open_browser_settings"),
                )],
            );
        }
        match self
            .credential_store
            .load_credential(caller_extension_id, install_instance_id)
        {
            Ok(Some(record))
                if validate_record_binding(&record, caller_extension_id, install_instance_id)
                    .is_ok() =>
            {
                pairing_status_value(
                    "paired",
                    Some((&record.server_base_origin, "Agent Zero")),
                    Vec::new(),
                )
            }
            Ok(Some(_)) => pairing_status_value(
                "repair_required",
                None,
                vec![diagnostic(
                    PairingFailure::ProfileBindingMismatch.code(),
                    PairingFailure::ProfileBindingMismatch.message(),
                    Some("open_browser_settings"),
                )],
            ),
            Ok(None) => pairing_status_value("unpaired", None, Vec::new()),
            Err(error) => pairing_status_value(
                "repair_required",
                None,
                vec![diagnostic(
                    error.code(),
                    error.message(),
                    Some("open_browser_settings"),
                )],
            ),
        }
    }

    pub(crate) fn exchange(
        &self,
        params: &Value,
        caller_extension_id: &str,
        install_instance_id: &str,
    ) -> Result<Value, PairingFailure> {
        let _lock = CredentialMutationLock::acquire(caller_extension_id, install_instance_id)?;
        let fields = params.as_object().ok_or(PairingFailure::ExchangeRejected)?;
        if fields.len() != 3
            || fields.keys().any(|key| {
                !matches!(
                    key.as_str(),
                    "contract_version" | "pairing_code" | "server_base_origin"
                )
            })
            || fields.get("contract_version").and_then(Value::as_u64) != Some(CONTRACT_VERSION)
        {
            return Err(PairingFailure::ExchangeRejected);
        }
        let pairing_code = fields
            .get("pairing_code")
            .and_then(Value::as_str)
            .filter(|value| valid_pairing_code(value))
            .ok_or(PairingFailure::ExchangeRejected)?;
        let server_base_origin = fields
            .get("server_base_origin")
            .and_then(Value::as_str)
            .filter(|value| valid_pairing_server_base_origin(value))
            .ok_or(PairingFailure::ExchangeRejected)?;
        if validate_profile_identity(caller_extension_id, install_instance_id).is_err() {
            return Err(PairingFailure::ExchangeRejected);
        }
        if let Some(record) = self
            .credential_store
            .load_credential(caller_extension_id, install_instance_id)?
        {
            validate_record_binding(&record, caller_extension_id, install_instance_id)?;
            return Err(PairingFailure::AlreadyPaired);
        }

        // Initialize and verify credential-store availability before the
        // one-time server exchange can create an orphaned public-key record.
        let companion_instance_id = self.companion_instance_id()?;
        let mut private_seed = Zeroizing::new([0_u8; PRIVATE_KEY_BYTES]);
        self.entropy.fill(&mut private_seed[..])?;
        let signing_key = SigningKey::from_bytes(&private_seed);
        let public_key = URL_SAFE_NO_PAD.encode(signing_key.verifying_key().to_bytes());
        drop(signing_key);

        let request = core_exchange_request(
            pairing_code,
            server_base_origin,
            caller_extension_id,
            &companion_instance_id,
            &public_key,
        );
        let endpoint = format!("{server_base_origin}{CORE_PAIRING_PATH}");
        let response_bytes = self
            .transport
            .exchange(&endpoint, request.encode().as_bytes())?;
        let response =
            json::parse(&response_bytes).map_err(|_| PairingFailure::ExchangeResponseInvalid)?;
        let success =
            parse_core_exchange_success(&response, server_base_origin, caller_extension_id)?;
        let credential = CredentialRecord {
            bridge_id: success.bridge_id.clone(),
            server_instance_id: success.server_instance_id.clone(),
            server_base_origin: success.server_base_origin.clone(),
            extension_id: caller_extension_id.to_owned(),
            install_instance_id: install_instance_id.to_owned(),
            companion_instance_id,
            key_generation: success.key_generation,
            created_at_ms: success.created_at_ms,
            private_seed,
        };
        self.credential_store
            .save_credential(&credential)
            .map_err(|_| PairingFailure::CredentialCommitFailed)?;
        Ok(native_pairing_success(&success))
    }

    pub(crate) fn disconnect(
        &self,
        caller_extension_id: &str,
        install_instance_id: &str,
    ) -> Result<Value, PairingFailure> {
        let _lock = CredentialMutationLock::acquire(caller_extension_id, install_instance_id)?;
        validate_profile_identity(caller_extension_id, install_instance_id)
            .map_err(|_| PairingFailure::ProfileBindingMismatch)?;
        let Some(record) = self
            .credential_store
            .load_credential(caller_extension_id, install_instance_id)?
        else {
            return Ok(pairing_status_value("unpaired", None, Vec::new()));
        };
        validate_record_binding(&record, caller_extension_id, install_instance_id)?;
        self.credential_store
            .delete_rotation(caller_extension_id, install_instance_id)?;
        self.credential_store
            .delete_credential(caller_extension_id, install_instance_id)?;
        Ok(pairing_status_value(
            "unpaired",
            None,
            vec![diagnostic(
                "SERVER_REVOCATION_REQUIRED",
                "The local key was deleted; revoke the stale bridge in Agent Zero when reachable.",
                Some("open_browser_settings"),
            )],
        ))
    }

    fn companion_instance_id(&self) -> Result<String, PairingFailure> {
        if let Some(identifier) = self.credential_store.load_installation_id()? {
            return Ok(format_uuid(identifier));
        }
        let mut identifier = [0_u8; INSTALLATION_ID_BYTES];
        self.entropy.fill(&mut identifier)?;
        identifier[6] = (identifier[6] & 0x0f) | 0x40;
        identifier[8] = (identifier[8] & 0x3f) | 0x80;
        self.credential_store.save_installation_id(&identifier)?;
        Ok(format_uuid(identifier))
    }
}

struct SystemEntropy;

// Cross-process mutation serialization prevents overlapping Chrome/native
// processes from replacing a pending key or deleting a newly committed key.
// The lock contains no credential material and the OS releases it on process exit.
#[cfg(test)]
struct CredentialMutationLock;
#[cfg(test)]
impl CredentialMutationLock {
    fn acquire(extension: &str, install: &str) -> Result<Self, PairingFailure> {
        profile_credential_slot(extension, install)?;
        Ok(Self)
    }
}

#[cfg(all(unix, not(test)))]
struct CredentialMutationLock {
    _file: std::fs::File,
}
#[cfg(all(unix, not(test)))]
impl CredentialMutationLock {
    fn acquire(extension: &str, install: &str) -> Result<Self, PairingFailure> {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
        unsafe extern "C" {
            fn geteuid() -> u32;
            fn flock(fd: i32, operation: i32) -> i32;
        }
        let uid = unsafe { geteuid() };
        let directory =
            std::env::temp_dir().join(format!("a0-browser-bridge-credential-locks-{uid}"));
        match std::fs::DirBuilder::new().mode(0o700).create(&directory) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(_) => return Err(PairingFailure::CredentialStoreUnavailable),
        }
        let metadata = std::fs::symlink_metadata(&directory)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)?;
        if !metadata.is_dir() || metadata.uid() != uid || metadata.mode() & 0o077 != 0 {
            return Err(PairingFailure::CredentialStoreUnavailable);
        }
        let path = directory.join(profile_credential_slot(extension, install)?);
        #[cfg(target_os = "macos")]
        const NOFOLLOW: i32 = 0x100;
        #[cfg(not(target_os = "macos"))]
        const NOFOLLOW: i32 = 0x20000;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(NOFOLLOW)
            .open(&path)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)?;
        let opened = file
            .metadata()
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)?;
        let linked = std::fs::symlink_metadata(&path)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)?;
        if !opened.is_file()
            || opened.uid() != uid
            || opened.nlink() != 1
            || opened.mode() & 0o077 != 0
            || opened.ino() != linked.ino()
            || opened.dev() != linked.dev()
            || unsafe { flock(file.as_raw_fd(), 2 | 4) } != 0
        {
            return Err(PairingFailure::CredentialStoreUnavailable);
        }
        Ok(Self { _file: file })
    }
}

#[cfg(all(windows, not(test)))]
struct CredentialMutationLock {
    handle: *mut std::ffi::c_void,
}
#[cfg(all(windows, not(test)))]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateMutexW(
        attributes: *mut std::ffi::c_void,
        initial_owner: i32,
        name: *const u16,
    ) -> *mut std::ffi::c_void;
    fn WaitForSingleObject(handle: *mut std::ffi::c_void, milliseconds: u32) -> u32;
    fn ReleaseMutex(handle: *mut std::ffi::c_void) -> i32;
    fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
}
#[cfg(all(windows, not(test)))]
impl CredentialMutationLock {
    fn acquire(extension: &str, install: &str) -> Result<Self, PairingFailure> {
        let name: Vec<u16> = format!(
            "Local\\A0BrowserBridgeCredential-{}\0",
            profile_credential_slot(extension, install)?
        )
        .encode_utf16()
        .collect();
        let handle = unsafe { CreateMutexW(std::ptr::null_mut(), 0, name.as_ptr()) };
        if handle.is_null() {
            return Err(PairingFailure::CredentialStoreUnavailable);
        }
        if !matches!(unsafe { WaitForSingleObject(handle, 0) }, 0 | 0x80) {
            unsafe {
                CloseHandle(handle);
            }
            return Err(PairingFailure::CredentialStoreUnavailable);
        }
        Ok(Self { handle })
    }
}
#[cfg(all(windows, not(test)))]
impl Drop for CredentialMutationLock {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.handle);
            CloseHandle(self.handle);
        }
    }
}

impl EntropySource for SystemEntropy {
    fn fill(&self, destination: &mut [u8]) -> Result<(), PairingFailure> {
        getrandom::fill(destination).map_err(|_| PairingFailure::EntropyUnavailable)
    }
}

#[cfg(not(test))]
struct SystemCredentialStore;

#[cfg(not(test))]
impl SystemCredentialStore {
    fn entry(name: &str) -> Result<keyring::Entry, PairingFailure> {
        keyring::Entry::new(CREDENTIAL_SERVICE, name)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)
    }

    fn legacy_credential_present() -> Result<bool, PairingFailure> {
        let mut value = match Self::entry(LEGACY_CREDENTIAL_ENTRY)?.get_secret() {
            Ok(value) => Zeroizing::new(value),
            Err(keyring::Error::NoEntry) => return Ok(false),
            Err(_) => return Err(PairingFailure::CredentialStoreUnavailable),
        };
        // Legacy bytes are deliberately neither decoded nor copied into a
        // profile slot. Their presence is only a re-pair-required signal.
        value.zeroize();
        Ok(true)
    }
}

#[cfg(not(test))]
impl CredentialStore for SystemCredentialStore {
    fn load_rotation(
        &self,
        extension: &str,
        install: &str,
    ) -> Result<Option<Zeroizing<Vec<u8>>>, PairingFailure> {
        let slot = format!("{}-rotation", profile_credential_slot(extension, install)?);
        match Self::entry(&slot)?.get_secret() {
            Ok(bytes) => Ok(Some(Zeroizing::new(bytes))),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err(PairingFailure::CredentialStoreUnavailable),
        }
    }

    fn save_rotation(
        &self,
        extension: &str,
        install: &str,
        bytes: &[u8],
    ) -> Result<(), PairingFailure> {
        let slot = format!("{}-rotation", profile_credential_slot(extension, install)?);
        Self::entry(&slot)?
            .set_secret(bytes)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)
    }

    fn delete_rotation(&self, extension: &str, install: &str) -> Result<(), PairingFailure> {
        let slot = format!("{}-rotation", profile_credential_slot(extension, install)?);
        match Self::entry(&slot)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(PairingFailure::CredentialStoreUnavailable),
        }
    }

    fn load_installation_id(&self) -> Result<Option<[u8; INSTALLATION_ID_BYTES]>, PairingFailure> {
        let entry = Self::entry(INSTALLATION_ENTRY)?;
        let mut value = match entry.get_secret() {
            Ok(value) => Zeroizing::new(value),
            Err(keyring::Error::NoEntry) => return Ok(None),
            Err(_) => return Err(PairingFailure::CredentialStoreUnavailable),
        };
        if value.len() != INSTALLATION_ID_BYTES {
            value.zeroize();
            return Err(PairingFailure::CredentialCorrupt);
        }
        let mut identifier = [0_u8; INSTALLATION_ID_BYTES];
        identifier.copy_from_slice(&value);
        Ok(Some(identifier))
    }

    fn save_installation_id(
        &self,
        installation_id: &[u8; INSTALLATION_ID_BYTES],
    ) -> Result<(), PairingFailure> {
        Self::entry(INSTALLATION_ENTRY)?
            .set_secret(installation_id)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)
    }

    fn load_credential(
        &self,
        extension_id: &str,
        install_instance_id: &str,
    ) -> Result<Option<CredentialRecord>, PairingFailure> {
        let slot = profile_credential_slot(extension_id, install_instance_id)?;
        let entry = Self::entry(&slot)?;
        let value = match entry.get_secret() {
            Ok(value) => Zeroizing::new(value),
            Err(keyring::Error::NoEntry) => {
                return if Self::legacy_credential_present()? {
                    Err(PairingFailure::LegacyCredentialPresent)
                } else {
                    Ok(None)
                }
            }
            Err(_) => return Err(PairingFailure::CredentialStoreUnavailable),
        };
        let record = decode_credential(&value)?;
        validate_record_binding(&record, extension_id, install_instance_id)?;
        Ok(Some(record))
    }

    fn save_credential(&self, credential: &CredentialRecord) -> Result<(), PairingFailure> {
        let encoded = encode_credential(credential)?;
        let slot =
            profile_credential_slot(&credential.extension_id, &credential.install_instance_id)?;
        Self::entry(&slot)?
            .set_secret(&encoded)
            .map_err(|_| PairingFailure::CredentialStoreUnavailable)
    }

    fn delete_credential(
        &self,
        extension_id: &str,
        install_instance_id: &str,
    ) -> Result<(), PairingFailure> {
        let slot = profile_credential_slot(extension_id, install_instance_id)?;
        let entry = Self::entry(&slot)?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err(PairingFailure::CredentialStoreUnavailable),
        }
    }
}

struct SystemPairingTransport {
    agent: ureq::Agent,
}

impl SystemPairingTransport {
    fn new() -> Self {
        let tls_config = ureq::tls::TlsConfig::builder()
            .root_certs(ureq::tls::RootCerts::PlatformVerifier)
            .build();
        let config = ureq::Agent::config_builder()
            .tls_config(tls_config)
            .max_redirects(0)
            .proxy(None)
            .timeout_global(Some(HTTP_TIMEOUT))
            .user_agent(format!("a0-browser-bridge/{COMPANION_VERSION}"))
            .build();
        Self {
            agent: ureq::Agent::new_with_config(config),
        }
    }
}

impl PairingTransport for SystemPairingTransport {
    fn exchange(&self, endpoint: &str, body: &[u8]) -> Result<Vec<u8>, PairingFailure> {
        let mut response = match self
            .agent
            .post(endpoint)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .send(body)
        {
            Ok(response) => response,
            Err(ureq::Error::StatusCode(status)) if (400..500).contains(&status) => {
                return Err(PairingFailure::ExchangeRejected)
            }
            Err(ureq::Error::StatusCode(_)) => return Err(PairingFailure::ExchangeOutcomeUnknown),
            Err(_) => return Err(PairingFailure::ExchangeOutcomeUnknown),
        };
        if response.status().as_u16() != 200
            || response
                .headers()
                .get("Content-Type")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.split(';').next())
                != Some("application/json")
        {
            return Err(PairingFailure::ExchangeResponseInvalid);
        }
        let body = response
            .body_mut()
            .with_config()
            .limit((MAX_CORE_RESPONSE_BYTES + 1) as u64)
            .read_to_vec()
            .map_err(|_| PairingFailure::ExchangeOutcomeUnknown)?;
        if body.len() > MAX_CORE_RESPONSE_BYTES {
            return Err(PairingFailure::ExchangeResponseInvalid);
        }
        Ok(body)
    }
}

struct CorePairingSuccess {
    bridge_id: String,
    server_instance_id: String,
    server_base_origin: String,
    key_generation: u32,
    created_at_ms: u64,
    scopes: Vec<String>,
    site_mode: String,
}

fn core_exchange_request(
    pairing_code: &str,
    server_base_origin: &str,
    extension_id: &str,
    companion_instance_id: &str,
    public_key: &str,
) -> Value {
    let mut key = BTreeMap::new();
    key.insert("algorithm".to_owned(), Value::String("Ed25519".to_owned()));
    key.insert(
        "encoding".to_owned(),
        Value::String("raw-base64url".to_owned()),
    );
    key.insert("value".to_owned(), Value::String(public_key.to_owned()));

    let mut request = BTreeMap::new();
    request.insert(
        "companion_instance_id".to_owned(),
        Value::String(companion_instance_id.to_owned()),
    );
    request.insert(
        "extension_id".to_owned(),
        Value::String(extension_id.to_owned()),
    );
    request.insert(
        "pairing_code".to_owned(),
        Value::String(pairing_code.to_owned()),
    );
    request.insert("public_key".to_owned(), Value::Object(key));
    request.insert(
        "server_base_url".to_owned(),
        Value::String(server_base_origin.to_owned()),
    );
    request.insert("trust_version".to_owned(), Value::Number("1".to_owned()));
    #[cfg(feature = "local-development")]
    request.insert(
        "channel".to_owned(),
        Value::String(crate::DEVELOPMENT_CHANNEL.to_owned()),
    );
    Value::Object(request)
}

fn parse_core_exchange_success(
    value: &Value,
    expected_base_origin: &str,
    expected_extension_id: &str,
) -> Result<CorePairingSuccess, PairingFailure> {
    let fields = value
        .as_object()
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    #[cfg(not(feature = "local-development"))]
    const KEYS: &[&str] = &[
        "contract",
        "trust_version",
        "state",
        "bridge_id",
        "server_instance_id",
        "server_base_url",
        "subject_id",
        "extension_id",
        "display_name",
        "key_generation",
        "scopes",
        "protocols",
        "policy",
        "created_at_ms",
        "native_runtime_location",
        "docker_install_target",
        "connector_session_ready",
        "browser_control_ready",
    ];
    #[cfg(feature = "local-development")]
    const KEYS: &[&str] = &[
        "contract",
        "trust_version",
        "channel",
        "state",
        "bridge_id",
        "server_instance_id",
        "server_base_url",
        "subject_id",
        "extension_id",
        "display_name",
        "key_generation",
        "scopes",
        "protocols",
        "policy",
        "created_at_ms",
        "native_runtime_location",
        "docker_install_target",
        "connector_session_ready",
        "browser_control_ready",
        "reason_code",
    ];
    if !has_exact_keys(fields, KEYS)
        || string(fields, "contract") != Some(TRUST_CONTRACT)
        || fields.get("trust_version").and_then(Value::as_u64) != Some(1)
        || string(fields, "state") != Some("paired")
        || string(fields, "subject_id") != Some("single_user")
        || string(fields, "extension_id") != Some(expected_extension_id)
        || string(fields, "server_base_url") != Some(expected_base_origin)
        || string(fields, "native_runtime_location") != Some("user_browser_host")
        || fields.get("docker_install_target") != Some(&Value::Bool(false))
        || fields.get("connector_session_ready") != Some(&Value::Bool(false))
        || fields.get("browser_control_ready") != Some(&Value::Bool(false))
    {
        return Err(PairingFailure::ExchangeResponseInvalid);
    }
    #[cfg(feature = "local-development")]
    if string(fields, "channel") != Some(crate::DEVELOPMENT_CHANNEL)
        || string(fields, "reason_code") != Some("development_runtime_not_available")
    {
        return Err(PairingFailure::ExchangeResponseInvalid);
    }
    let bridge_id = opaque(fields, "bridge_id")?.to_owned();
    let server_instance_id = opaque(fields, "server_instance_id")?.to_owned();
    let display_name = string(fields, "display_name")
        .filter(|value| !value.is_empty() && value.len() <= 192)
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    if display_name.chars().any(char::is_control) {
        return Err(PairingFailure::ExchangeResponseInvalid);
    }
    let key_generation = fields
        .get("key_generation")
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .filter(|value| *value == 1)
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    let created_at_ms = fields
        .get("created_at_ms")
        .and_then(Value::as_u64)
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    let scopes = parse_scopes(fields.get("scopes"))?;
    validate_protocols(fields.get("protocols"))?;
    let site_mode = validate_policy(fields.get("policy"))?;
    Ok(CorePairingSuccess {
        bridge_id,
        server_instance_id,
        server_base_origin: expected_base_origin.to_owned(),
        key_generation,
        created_at_ms,
        scopes,
        site_mode,
    })
}

fn parse_scopes(value: Option<&Value>) -> Result<Vec<String>, PairingFailure> {
    let values = value
        .and_then(Value::as_array)
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    if values.len() != FIXED_SCOPES.len()
        || values
            .iter()
            .zip(FIXED_SCOPES)
            .any(|(value, expected)| value.as_str() != Some(*expected))
    {
        return Err(PairingFailure::ExchangeResponseInvalid);
    }
    Ok(FIXED_SCOPES
        .iter()
        .map(|value| (*value).to_owned())
        .collect())
}

fn validate_protocols(value: Option<&Value>) -> Result<(), PairingFailure> {
    let protocols = value
        .and_then(Value::as_object)
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    if !has_exact_keys(
        protocols,
        &["connector", "browser", "adapter", "mv3_runtime"],
    ) || string(protocols, "connector") != Some(CONNECTOR_PROTOCOL)
        || string(protocols, "browser") != Some(BROWSER_PROTOCOL)
        || string(protocols, "adapter") != Some(ADAPTER_CONTRACT)
        || string(protocols, "mv3_runtime") != Some(MV3_RUNTIME_CONTRACT)
    {
        return Err(PairingFailure::ExchangeResponseInvalid);
    }
    Ok(())
}

fn validate_policy(value: Option<&Value>) -> Result<String, PairingFailure> {
    let policy = value
        .and_then(Value::as_object)
        .ok_or(PairingFailure::ExchangeResponseInvalid)?;
    if !has_exact_keys(policy, &["site_mode"])
        || string(policy, "site_mode") != Some("ask_per_site")
    {
        return Err(PairingFailure::ExchangeResponseInvalid);
    }
    Ok("ask_per_site".to_owned())
}

fn native_pairing_success(success: &CorePairingSuccess) -> Value {
    let mut server = BTreeMap::new();
    server.insert(
        "instance_id".to_owned(),
        Value::String(success.server_instance_id.clone()),
    );
    server.insert("label".to_owned(), Value::String("Agent Zero".to_owned()));
    server.insert(
        "base_origin".to_owned(),
        Value::String(success.server_base_origin.clone()),
    );

    let mut policy = BTreeMap::new();
    policy.insert("mode".to_owned(), Value::String(success.site_mode.clone()));
    policy.insert("ready".to_owned(), Value::Bool(false));

    let mut result = BTreeMap::new();
    result.insert(
        "contract_version".to_owned(),
        Value::Number(CONTRACT_VERSION.to_string()),
    );
    result.insert("state".to_owned(), Value::String("paired".to_owned()));
    result.insert(
        "bridge_id".to_owned(),
        Value::String(success.bridge_id.clone()),
    );
    result.insert("server".to_owned(), Value::Object(server));
    result.insert(
        "scopes".to_owned(),
        Value::Array(
            success
                .scopes
                .iter()
                .map(|scope| Value::String(scope.clone()))
                .collect(),
        ),
    );
    result.insert("policy".to_owned(), Value::Object(policy));
    add_development_projection(&mut result);
    Value::Object(result)
}

fn pairing_status_value(
    state: &str,
    server: Option<(&str, &str)>,
    diagnostics: Vec<Value>,
) -> Value {
    let server = server.map_or(Value::Null, |(base_origin, label)| {
        let mut fields = BTreeMap::new();
        fields.insert("label".to_owned(), Value::String(label.to_owned()));
        fields.insert(
            "base_origin".to_owned(),
            Value::String(base_origin.to_owned()),
        );
        Value::Object(fields)
    });
    let mut companion = BTreeMap::new();
    companion.insert(
        "version".to_owned(),
        Value::String(COMPANION_VERSION.to_owned()),
    );
    let mut result = BTreeMap::new();
    result.insert(
        "contract_version".to_owned(),
        Value::Number(CONTRACT_VERSION.to_string()),
    );
    result.insert("state".to_owned(), Value::String(state.to_owned()));
    result.insert("server".to_owned(), server);
    result.insert("companion".to_owned(), Value::Object(companion));
    result.insert("diagnostics".to_owned(), Value::Array(diagnostics));
    add_development_projection(&mut result);
    Value::Object(result)
}

#[cfg(not(feature = "local-development"))]
fn add_development_projection(_result: &mut BTreeMap<String, Value>) {}

#[cfg(feature = "local-development")]
fn add_development_projection(result: &mut BTreeMap<String, Value>) {
    result.insert("development".to_owned(), development_projection());
}

#[cfg(feature = "local-development")]
pub(crate) fn development_projection() -> Value {
    Value::Object(BTreeMap::from([
        (
            "contract".to_owned(),
            Value::String(crate::DEVELOPMENT_TRUST_CONTRACT.to_owned()),
        ),
        (
            "channel".to_owned(),
            Value::String(crate::DEVELOPMENT_CHANNEL.to_owned()),
        ),
        ("connector_session_ready".to_owned(), Value::Bool(false)),
        ("browser_control_ready".to_owned(), Value::Bool(false)),
        (
            "reason_code".to_owned(),
            Value::String("development_runtime_not_available".to_owned()),
        ),
    ]))
}

fn diagnostic(code: &str, message: &str, action: Option<&str>) -> Value {
    let mut fields = BTreeMap::new();
    fields.insert("code".to_owned(), Value::String(code.to_owned()));
    fields.insert("message".to_owned(), Value::String(message.to_owned()));
    fields.insert(
        "action".to_owned(),
        action.map_or(Value::Null, |value| Value::String(value.to_owned())),
    );
    Value::Object(fields)
}

fn validate_profile_identity(
    extension_id: &str,
    install_instance_id: &str,
) -> Result<(), PairingFailure> {
    if !is_extension_id(extension_id) || !valid_opaque_id(install_instance_id) {
        return Err(PairingFailure::ProfileBindingMismatch);
    }
    #[cfg(feature = "local-development")]
    if extension_id != crate::DEVELOPMENT_EXTENSION_ID {
        return Err(PairingFailure::ExtensionBindingMismatch);
    }
    Ok(())
}

#[cfg(not(feature = "local-development"))]
fn valid_pairing_server_base_origin(value: &str) -> bool {
    valid_server_base_origin(value)
}

#[cfg(feature = "local-development")]
fn valid_pairing_server_base_origin(value: &str) -> bool {
    valid_development_server_base_origin(value)
}

fn validate_record_binding(
    record: &CredentialRecord,
    extension_id: &str,
    install_instance_id: &str,
) -> Result<(), PairingFailure> {
    if record.extension_id != extension_id {
        return Err(PairingFailure::ExtensionBindingMismatch);
    }
    if record.install_instance_id != install_instance_id {
        return Err(PairingFailure::ProfileBindingMismatch);
    }
    Ok(())
}

fn profile_credential_slot(
    extension_id: &str,
    install_instance_id: &str,
) -> Result<String, PairingFailure> {
    validate_profile_identity(extension_id, install_instance_id)
        .map_err(|_| PairingFailure::CredentialCorrupt)?;
    let mut hasher = Sha256::new();
    #[cfg(not(feature = "local-development"))]
    hasher.update(b"a0-browser-bridge-profile-v2\0");
    #[cfg(feature = "local-development")]
    hasher.update(b"a0-browser-bridge-development-profile-v2\0");
    hasher.update(extension_id.as_bytes());
    hasher.update([0]);
    hasher.update(install_instance_id.as_bytes());
    let digest = hasher.finalize();
    let mut suffix = String::with_capacity(64);
    for byte in digest {
        write!(&mut suffix, "{byte:02x}").map_err(|_| PairingFailure::CredentialCorrupt)?;
    }
    Ok(format!("{PROFILE_CREDENTIAL_PREFIX}{suffix}"))
}

fn encode_credential(record: &CredentialRecord) -> Result<Zeroizing<Vec<u8>>, PairingFailure> {
    if !valid_opaque_id(&record.bridge_id)
        || !valid_opaque_id(&record.server_instance_id)
        || !valid_pairing_server_base_origin(&record.server_base_origin)
        || !is_extension_id(&record.extension_id)
        || !valid_opaque_id(&record.install_instance_id)
        || !valid_opaque_id(&record.companion_instance_id)
        || !(1..=MAX_KEY_GENERATION).contains(&record.key_generation)
    {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let mut encoded = Zeroizing::new(Vec::with_capacity(2_560));
    encoded.extend_from_slice(CREDENTIAL_MAGIC);
    encoded.push(CREDENTIAL_FORMAT_VERSION);
    encoded.extend_from_slice(&record.key_generation.to_be_bytes());
    encoded.extend_from_slice(&record.created_at_ms.to_be_bytes());
    for value in [
        &record.bridge_id,
        &record.server_instance_id,
        &record.server_base_origin,
        &record.extension_id,
        &record.install_instance_id,
        &record.companion_instance_id,
    ] {
        write_field(&mut encoded, value)?;
    }
    encoded.extend_from_slice(&record.private_seed[..]);
    Ok(encoded)
}

fn decode_credential(encoded: &[u8]) -> Result<CredentialRecord, PairingFailure> {
    let mut decoder = CredentialDecoder { encoded, index: 0 };
    if decoder.read(CREDENTIAL_MAGIC.len())? != CREDENTIAL_MAGIC {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let version = decoder.read_u8()?;
    if version == LEGACY_CREDENTIAL_FORMAT_VERSION {
        return Err(PairingFailure::LegacyCredentialPresent);
    }
    if version != CREDENTIAL_FORMAT_VERSION {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let key_generation = decoder.read_u32()?;
    let created_at_ms = decoder.read_u64()?;
    let bridge_id = decoder.read_string(256)?;
    let server_instance_id = decoder.read_string(256)?;
    let server_base_origin = decoder.read_string(2_048)?;
    let extension_id = decoder.read_string(32)?;
    let install_instance_id = decoder.read_string(256)?;
    let companion_instance_id = decoder.read_string(256)?;
    let mut private_seed = Zeroizing::new([0_u8; PRIVATE_KEY_BYTES]);
    private_seed.copy_from_slice(decoder.read(PRIVATE_KEY_BYTES)?);
    if decoder.index != encoded.len()
        || !valid_opaque_id(&bridge_id)
        || !valid_opaque_id(&server_instance_id)
        || !valid_pairing_server_base_origin(&server_base_origin)
        || !is_extension_id(&extension_id)
        || !valid_opaque_id(&install_instance_id)
        || !valid_opaque_id(&companion_instance_id)
        || !(1..=MAX_KEY_GENERATION).contains(&key_generation)
    {
        return Err(PairingFailure::CredentialCorrupt);
    }
    Ok(CredentialRecord {
        bridge_id,
        server_instance_id,
        server_base_origin,
        extension_id,
        install_instance_id,
        companion_instance_id,
        key_generation,
        created_at_ms,
        private_seed,
    })
}

fn encode_rotation(pending: &PendingRotation) -> Result<Zeroizing<Vec<u8>>, PairingFailure> {
    if !valid_opaque_id(&pending.rotation_id) {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let mut bytes = Zeroizing::new(Vec::new());
    bytes.extend_from_slice(PENDING_ROTATION_MAGIC);
    write_field(&mut bytes, &pending.rotation_id)?;
    bytes.extend_from_slice(&encode_credential(&pending.credential)?);
    Ok(bytes)
}

fn decode_rotation(bytes: &[u8]) -> Result<PendingRotation, PairingFailure> {
    if bytes.len() > 4096 {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let mut decoder = CredentialDecoder {
        encoded: bytes,
        index: 0,
    };
    if decoder.read(8)? != PENDING_ROTATION_MAGIC {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let rotation_id = decoder.read_string(256)?;
    if !valid_opaque_id(&rotation_id) {
        return Err(PairingFailure::CredentialCorrupt);
    }
    let credential = decode_credential(&bytes[decoder.index..])?;
    Ok(PendingRotation {
        rotation_id,
        credential,
    })
}

fn write_field(destination: &mut Vec<u8>, value: &str) -> Result<(), PairingFailure> {
    let length = u16::try_from(value.len()).map_err(|_| PairingFailure::CredentialCorrupt)?;
    destination.extend_from_slice(&length.to_be_bytes());
    destination.extend_from_slice(value.as_bytes());
    Ok(())
}

struct CredentialDecoder<'a> {
    encoded: &'a [u8],
    index: usize,
}

impl<'a> CredentialDecoder<'a> {
    fn read(&mut self, length: usize) -> Result<&'a [u8], PairingFailure> {
        let end = self
            .index
            .checked_add(length)
            .ok_or(PairingFailure::CredentialCorrupt)?;
        let value = self
            .encoded
            .get(self.index..end)
            .ok_or(PairingFailure::CredentialCorrupt)?;
        self.index = end;
        Ok(value)
    }

    fn read_u8(&mut self) -> Result<u8, PairingFailure> {
        Ok(self.read(1)?[0])
    }

    fn read_u32(&mut self) -> Result<u32, PairingFailure> {
        let bytes: [u8; 4] = self
            .read(4)?
            .try_into()
            .map_err(|_| PairingFailure::CredentialCorrupt)?;
        Ok(u32::from_be_bytes(bytes))
    }

    fn read_u64(&mut self) -> Result<u64, PairingFailure> {
        let bytes: [u8; 8] = self
            .read(8)?
            .try_into()
            .map_err(|_| PairingFailure::CredentialCorrupt)?;
        Ok(u64::from_be_bytes(bytes))
    }

    fn read_string(&mut self, maximum: usize) -> Result<String, PairingFailure> {
        let length_bytes: [u8; 2] = self
            .read(2)?
            .try_into()
            .map_err(|_| PairingFailure::CredentialCorrupt)?;
        let length = u16::from_be_bytes(length_bytes) as usize;
        if length == 0 || length > maximum {
            return Err(PairingFailure::CredentialCorrupt);
        }
        std::str::from_utf8(self.read(length)?)
            .map(str::to_owned)
            .map_err(|_| PairingFailure::CredentialCorrupt)
    }
}

fn has_exact_keys(fields: &BTreeMap<String, Value>, expected: &[&str]) -> bool {
    fields.len() == expected.len() && expected.iter().all(|key| fields.contains_key(*key))
}

fn string<'a>(fields: &'a BTreeMap<String, Value>, key: &str) -> Option<&'a str> {
    fields.get(key).and_then(Value::as_str)
}

fn opaque<'a>(fields: &'a BTreeMap<String, Value>, key: &str) -> Result<&'a str, PairingFailure> {
    string(fields, key)
        .filter(|value| valid_opaque_id(value))
        .ok_or(PairingFailure::ExchangeResponseInvalid)
}

fn format_uuid(bytes: [u8; INSTALLATION_ID_BYTES]) -> String {
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15],
    )
}

#[cfg(test)]
struct EmptyCredentialStore;

#[cfg(test)]
impl CredentialStore for EmptyCredentialStore {
    fn load_installation_id(&self) -> Result<Option<[u8; INSTALLATION_ID_BYTES]>, PairingFailure> {
        Ok(None)
    }

    fn save_installation_id(
        &self,
        _installation_id: &[u8; INSTALLATION_ID_BYTES],
    ) -> Result<(), PairingFailure> {
        Err(PairingFailure::BackendUnavailable)
    }

    fn load_credential(
        &self,
        _extension_id: &str,
        _install_instance_id: &str,
    ) -> Result<Option<CredentialRecord>, PairingFailure> {
        Ok(None)
    }

    fn save_credential(&self, _credential: &CredentialRecord) -> Result<(), PairingFailure> {
        Err(PairingFailure::BackendUnavailable)
    }

    fn delete_credential(
        &self,
        _extension_id: &str,
        _install_instance_id: &str,
    ) -> Result<(), PairingFailure> {
        Ok(())
    }
}

#[cfg(test)]
struct UnavailableTransport;

#[cfg(test)]
impl PairingTransport for UnavailableTransport {
    fn exchange(&self, _endpoint: &str, _body: &[u8]) -> Result<Vec<u8>, PairingFailure> {
        Err(PairingFailure::BackendUnavailable)
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};

    use super::*;

    #[test]
    fn staged_rotation_retains_active_until_exact_authenticated_commit() {
        let store = MemoryStore::empty();
        let mut active = CredentialRecord::fixture("https://agent.example");
        active.private_seed = Zeroizing::new([8; 32]);
        store.save_credential(&active).unwrap();
        let service = PairingService {
            credential_store: Box::new(store),
            transport: Box::new(UnavailableTransport),
            entropy: Box::new(FixedEntropy),
        };
        let first = service
            .stage_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap();
        assert_eq!(
            first,
            service
                .stage_rotation(&active.extension_id, &active.install_instance_id)
                .unwrap()
        );
        assert_eq!(
            service
                .connector_credential(&active.extension_id, &active.install_instance_id)
                .unwrap()
                .unwrap()
                .key_generation(),
            1
        );
        let pending = service
            .pending_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap()
            .unwrap();
        assert_eq!(pending.credential.key_generation(), 2);
        assert!(service
            .commit_rotation_authenticated(
                &active.extension_id,
                &active.install_instance_id,
                "wrong-rotation",
                2
            )
            .is_err());
        assert!(service
            .commit_rotation_authenticated(
                &active.extension_id,
                &active.install_instance_id,
                &pending.rotation_id,
                1
            )
            .is_err());
        service
            .commit_rotation_authenticated(
                &active.extension_id,
                &active.install_instance_id,
                &pending.rotation_id,
                2,
            )
            .unwrap();
        assert_eq!(
            service
                .connector_credential(&active.extension_id, &active.install_instance_id)
                .unwrap()
                .unwrap()
                .key_generation(),
            2
        );
        assert!(service
            .pending_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap()
            .is_none());
        assert!(service
            .revoke_authenticated(
                &active.extension_id,
                &active.install_instance_id,
                "old-bridge",
                &active.server_instance_id,
                2
            )
            .is_err());
        assert_eq!(
            service
                .connector_credential(&active.extension_id, &active.install_instance_id)
                .unwrap()
                .unwrap()
                .key_generation(),
            2
        );
    }

    #[test]
    fn expired_rotation_preserves_active_and_commit_crash_is_recoverable() {
        let store = MemoryStore::empty();
        let active = CredentialRecord::fixture("https://agent.example");
        store.save_credential(&active).unwrap();
        let service = PairingService {
            credential_store: Box::new(store),
            transport: Box::new(UnavailableTransport),
            entropy: Box::new(FixedEntropy),
        };
        service
            .stage_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap();
        let pending = service
            .pending_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap()
            .unwrap();
        assert!(service
            .expire_rotation(&active.extension_id, &active.install_instance_id, "other")
            .is_err());
        service
            .expire_rotation(
                &active.extension_id,
                &active.install_instance_id,
                &pending.rotation_id,
            )
            .unwrap();
        assert_eq!(
            service
                .connector_credential(&active.extension_id, &active.install_instance_id)
                .unwrap()
                .unwrap()
                .key_generation(),
            1
        );
        service
            .stage_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap();
        let pending = service
            .pending_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap()
            .unwrap();
        service
            .credential_store
            .save_credential(&pending.credential)
            .unwrap();
        service
            .commit_rotation_authenticated(
                &active.extension_id,
                &active.install_instance_id,
                &pending.rotation_id,
                2,
            )
            .unwrap();
        assert!(service
            .pending_rotation(&active.extension_id, &active.install_instance_id)
            .unwrap()
            .is_none());
    }

    #[cfg(not(feature = "local-development"))]
    const EXTENSION_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    #[cfg(feature = "local-development")]
    const EXTENSION_ID: &str = crate::DEVELOPMENT_EXTENSION_ID;
    const INSTALL_A: &str = "install-a";
    const INSTALL_B: &str = "install-b";
    const PAIRING_CODE: &str = "A0B1-DEADBEEF-0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    const BASE_ORIGIN: &str = "http://localhost:50080";

    struct MemoryStore {
        installation_id: Mutex<Option<[u8; INSTALLATION_ID_BYTES]>>,
        credentials: Mutex<BTreeMap<String, Vec<u8>>>,
        legacy_credential: Mutex<Option<Vec<u8>>>,
    }

    impl MemoryStore {
        fn empty() -> Self {
            Self {
                installation_id: Mutex::new(None),
                credentials: Mutex::new(BTreeMap::new()),
                legacy_credential: Mutex::new(None),
            }
        }

        fn with_legacy(credential: Vec<u8>) -> Self {
            Self {
                installation_id: Mutex::new(None),
                credentials: Mutex::new(BTreeMap::new()),
                legacy_credential: Mutex::new(Some(credential)),
            }
        }
    }

    impl CredentialStore for MemoryStore {
        fn load_rotation(
            &self,
            extension: &str,
            install: &str,
        ) -> Result<Option<Zeroizing<Vec<u8>>>, PairingFailure> {
            let slot = format!("{}-rotation", profile_credential_slot(extension, install)?);
            Ok(self
                .credentials
                .lock()
                .unwrap()
                .get(&slot)
                .cloned()
                .map(Zeroizing::new))
        }
        fn save_rotation(
            &self,
            extension: &str,
            install: &str,
            bytes: &[u8],
        ) -> Result<(), PairingFailure> {
            let slot = format!("{}-rotation", profile_credential_slot(extension, install)?);
            self.credentials
                .lock()
                .unwrap()
                .insert(slot, bytes.to_vec());
            Ok(())
        }
        fn delete_rotation(&self, extension: &str, install: &str) -> Result<(), PairingFailure> {
            let slot = format!("{}-rotation", profile_credential_slot(extension, install)?);
            self.credentials.lock().unwrap().remove(&slot);
            Ok(())
        }
        fn load_installation_id(
            &self,
        ) -> Result<Option<[u8; INSTALLATION_ID_BYTES]>, PairingFailure> {
            Ok(*self.installation_id.lock().unwrap())
        }

        fn save_installation_id(
            &self,
            installation_id: &[u8; INSTALLATION_ID_BYTES],
        ) -> Result<(), PairingFailure> {
            *self.installation_id.lock().unwrap() = Some(*installation_id);
            Ok(())
        }

        fn load_credential(
            &self,
            extension_id: &str,
            install_instance_id: &str,
        ) -> Result<Option<CredentialRecord>, PairingFailure> {
            let slot = profile_credential_slot(extension_id, install_instance_id)?;
            if let Some(encoded) = self.credentials.lock().unwrap().get(&slot).cloned() {
                return decode_credential(&encoded).map(Some);
            }
            if self.legacy_credential.lock().unwrap().is_some() {
                return Err(PairingFailure::LegacyCredentialPresent);
            }
            Ok(None)
        }

        fn save_credential(&self, credential: &CredentialRecord) -> Result<(), PairingFailure> {
            let slot =
                profile_credential_slot(&credential.extension_id, &credential.install_instance_id)?;
            self.credentials
                .lock()
                .unwrap()
                .insert(slot, encode_credential(credential)?.to_vec());
            Ok(())
        }

        fn delete_credential(
            &self,
            extension_id: &str,
            install_instance_id: &str,
        ) -> Result<(), PairingFailure> {
            let slot = profile_credential_slot(extension_id, install_instance_id)?;
            self.credentials.lock().unwrap().remove(&slot);
            Ok(())
        }
    }

    struct FixedEntropy;

    impl EntropySource for FixedEntropy {
        fn fill(&self, destination: &mut [u8]) -> Result<(), PairingFailure> {
            destination.fill(7);
            Ok(())
        }
    }

    struct FixtureTransport {
        response: Vec<u8>,
        observed: Arc<Mutex<Option<(String, Vec<u8>)>>>,
    }

    struct SequenceTransport {
        responses: Mutex<VecDeque<Vec<u8>>>,
    }

    impl PairingTransport for SequenceTransport {
        fn exchange(&self, _endpoint: &str, _body: &[u8]) -> Result<Vec<u8>, PairingFailure> {
            self.responses
                .lock()
                .unwrap()
                .pop_front()
                .ok_or(PairingFailure::ExchangeRejected)
        }
    }

    impl PairingTransport for FixtureTransport {
        fn exchange(&self, endpoint: &str, body: &[u8]) -> Result<Vec<u8>, PairingFailure> {
            *self.observed.lock().unwrap() = Some((endpoint.to_owned(), body.to_vec()));
            Ok(self.response.clone())
        }
    }

    #[cfg(not(feature = "local-development"))]
    fn success_response_for(bridge_id: &str) -> Vec<u8> {
        format!(
            concat!(
                "{{\"contract\":\"a0.browser-bridge.trust.v1\",\"trust_version\":1,",
                "\"state\":\"paired\",\"bridge_id\":\"{BRIDGE_ID}\",",
                "\"server_instance_id\":\"server-1\",\"server_base_url\":\"{BASE_ORIGIN}\",",
                "\"subject_id\":\"single_user\",\"extension_id\":\"{EXTENSION_ID}\",",
                "\"display_name\":\"Taylor's browser host\",\"key_generation\":1,",
                "\"scopes\":[\"bridge.connect\",\"context.list\",\"context.read\",",
                "\"context.message\",\"browser.operate\",\"browser.control\",",
                "\"browser.artifact\",\"browser.approval\"],",
                "\"protocols\":{{\"connector\":\"a0-connector.v1\",",
                "\"browser\":\"a0.browser-bridge.v1\",",
                "\"adapter\":\"a0.browser-bridge.adapter.v1\",",
                "\"mv3_runtime\":\"a0.browser-bridge.mv3-runtime.v1\"}},",
                "\"policy\":{{\"site_mode\":\"ask_per_site\"}},",
                "\"created_at_ms\":1788492400000,",
                "\"native_runtime_location\":\"user_browser_host\",",
                "\"docker_install_target\":false,",
                "\"connector_session_ready\":false,\"browser_control_ready\":false}}"
            ),
            BASE_ORIGIN = BASE_ORIGIN,
            EXTENSION_ID = EXTENSION_ID,
            BRIDGE_ID = bridge_id,
        )
        .into_bytes()
    }

    #[cfg(feature = "local-development")]
    fn success_response_for(bridge_id: &str) -> Vec<u8> {
        // This is the exact public projection emitted by Core's development
        // exchange endpoint after it stores the separately namespaced record.
        format!(
            concat!(
                "{{\"contract\":\"a0.browser-bridge.development-trust.v1\",",
                "\"trust_version\":1,\"channel\":\"local-development\",",
                "\"state\":\"paired\",\"bridge_id\":\"{BRIDGE_ID}\",",
                "\"server_instance_id\":\"server-1\",\"server_base_url\":\"{BASE_ORIGIN}\",",
                "\"subject_id\":\"single_user\",\"extension_id\":\"{EXTENSION_ID}\",",
                "\"display_name\":\"Taylor's browser host\",\"key_generation\":1,",
                "\"scopes\":[\"bridge.connect\",\"context.list\",\"context.read\",",
                "\"context.message\",\"browser.operate\",\"browser.control\",",
                "\"browser.artifact\",\"browser.approval\"],",
                "\"protocols\":{{\"connector\":\"a0-connector.v1\",",
                "\"browser\":\"a0.browser-bridge.v1\",",
                "\"adapter\":\"a0.browser-bridge.adapter.v1\",",
                "\"mv3_runtime\":\"a0.browser-bridge.mv3-runtime.v1\"}},",
                "\"policy\":{{\"site_mode\":\"ask_per_site\"}},",
                "\"created_at_ms\":1788492400000,",
                "\"native_runtime_location\":\"user_browser_host\",",
                "\"docker_install_target\":false,",
                "\"connector_session_ready\":false,\"browser_control_ready\":false,",
                "\"reason_code\":\"development_runtime_not_available\"}}"
            ),
            BASE_ORIGIN = BASE_ORIGIN,
            EXTENSION_ID = EXTENSION_ID,
            BRIDGE_ID = bridge_id,
        )
        .into_bytes()
    }

    fn success_response() -> Vec<u8> {
        success_response_for("bridge-1")
    }

    fn exchange_params() -> Value {
        let mut fields = BTreeMap::new();
        fields.insert("contract_version".to_owned(), Value::Number("1".to_owned()));
        fields.insert(
            "pairing_code".to_owned(),
            Value::String(PAIRING_CODE.to_owned()),
        );
        fields.insert(
            "server_base_origin".to_owned(),
            Value::String(BASE_ORIGIN.to_owned()),
        );
        Value::Object(fields)
    }

    fn legacy_credential_bytes(record: &CredentialRecord) -> Vec<u8> {
        let mut encoded = Vec::new();
        encoded.extend_from_slice(CREDENTIAL_MAGIC);
        encoded.push(LEGACY_CREDENTIAL_FORMAT_VERSION);
        encoded.extend_from_slice(&record.key_generation.to_be_bytes());
        encoded.extend_from_slice(&record.created_at_ms.to_be_bytes());
        for value in [
            &record.bridge_id,
            &record.server_instance_id,
            &record.server_base_origin,
            &record.extension_id,
            &record.companion_instance_id,
        ] {
            write_field(&mut encoded, value).unwrap();
        }
        encoded.extend_from_slice(&record.private_seed[..]);
        encoded
    }

    #[test]
    fn successful_exchange_posts_only_public_key_and_commits_private_seed() {
        let observed = Arc::new(Mutex::new(None));
        let transport = FixtureTransport {
            response: success_response(),
            observed: Arc::clone(&observed),
        };
        let service = PairingService {
            credential_store: Box::new(MemoryStore::empty()),
            transport: Box::new(transport),
            entropy: Box::new(FixedEntropy),
        };

        let result = service
            .exchange(&exchange_params(), EXTENSION_ID, INSTALL_A)
            .unwrap();
        let encoded = result.encode();
        assert!(encoded.contains("\"state\":\"paired\""));
        assert!(encoded.contains("\"ready\":false"));
        #[cfg(feature = "local-development")]
        {
            assert!(encoded.contains("\"contract\":\"a0.browser-bridge.development-trust.v1\""));
            assert!(encoded.contains("\"reason_code\":\"development_runtime_not_available\""));
        }
        assert!(!encoded.contains("private"));
        assert!(!encoded.contains("pairing_code"));

        let (endpoint, request) = observed.lock().unwrap().clone().unwrap();
        assert_eq!(endpoint, format!("{BASE_ORIGIN}{CORE_PAIRING_PATH}"));
        let request = std::str::from_utf8(&request).unwrap();
        assert!(request.contains(PAIRING_CODE));
        assert!(request.contains("\"algorithm\":\"Ed25519\""));
        assert!(request.contains("\"encoding\":\"raw-base64url\""));
        #[cfg(feature = "local-development")]
        assert!(request.contains("\"channel\":\"local-development\""));
        assert!(!request.contains("private"));

        let hello = service.hello(EXTENSION_ID, INSTALL_A);
        assert_eq!(hello.server_state, "paired");
        assert_eq!(hello.server_instance_id.as_deref(), Some("server-1"));
        let status = service.status(EXTENSION_ID, INSTALL_A).encode();
        assert!(status.contains("\"state\":\"paired\""));
        assert!(!status.contains("private"));
        assert!(!status.contains(PAIRING_CODE));
    }

    #[test]
    fn credential_round_trip_is_strict_and_secret_output_is_redacted() {
        let record = CredentialRecord {
            bridge_id: "bridge-1".to_owned(),
            server_instance_id: "server-1".to_owned(),
            server_base_origin: BASE_ORIGIN.to_owned(),
            extension_id: EXTENSION_ID.to_owned(),
            install_instance_id: INSTALL_A.to_owned(),
            companion_instance_id: "07070707-0707-4707-8707-070707070707".to_owned(),
            key_generation: 1,
            created_at_ms: 1_788_492_400_000,
            private_seed: Zeroizing::new([9_u8; PRIVATE_KEY_BYTES]),
        };
        let encoded = encode_credential(&record).unwrap();
        let decoded = decode_credential(&encoded).unwrap();
        assert_eq!(decoded.bridge_id, record.bridge_id);
        assert_eq!(decoded.install_instance_id, record.install_instance_id);
        assert_eq!(decoded.private_seed[..], record.private_seed[..]);

        let mut trailing = encoded.to_vec();
        trailing.push(0);
        assert!(matches!(
            decode_credential(&trailing),
            Err(PairingFailure::CredentialCorrupt)
        ));
    }

    #[test]
    fn invalid_success_response_is_never_committed_or_retried() {
        let service = PairingService {
            credential_store: Box::new(MemoryStore::empty()),
            transport: Box::new(FixtureTransport {
                response: b"{\"state\":\"paired\",\"private_key\":\"forbidden\"}".to_vec(),
                observed: Arc::new(Mutex::new(None)),
            }),
            entropy: Box::new(FixedEntropy),
        };
        assert_eq!(
            service.exchange(&exchange_params(), EXTENSION_ID, INSTALL_A),
            Err(PairingFailure::ExchangeResponseInvalid)
        );
        assert_eq!(
            service.hello(EXTENSION_ID, INSTALL_A).server_state,
            "unpaired"
        );
    }

    #[test]
    fn local_disconnect_deletes_key_and_requires_server_revocation() {
        let service = PairingService {
            credential_store: Box::new(MemoryStore::empty()),
            transport: Box::new(FixtureTransport {
                response: success_response(),
                observed: Arc::new(Mutex::new(None)),
            }),
            entropy: Box::new(FixedEntropy),
        };
        service
            .exchange(&exchange_params(), EXTENSION_ID, INSTALL_A)
            .unwrap();
        let status = service
            .disconnect(EXTENSION_ID, INSTALL_A)
            .unwrap()
            .encode();
        assert!(status.contains("SERVER_REVOCATION_REQUIRED"));
        assert_eq!(
            service.hello(EXTENSION_ID, INSTALL_A).server_state,
            "unpaired"
        );
        let repeated = service
            .disconnect(EXTENSION_ID, INSTALL_A)
            .unwrap()
            .encode();
        assert!(repeated.contains("\"state\":\"unpaired\""));
        assert!(!repeated.contains("SERVER_REVOCATION_REQUIRED"));
    }

    #[cfg(not(feature = "local-development"))]
    #[test]
    fn independently_scoped_extensions_do_not_observe_or_delete_each_other() {
        let service = PairingService {
            credential_store: Box::new(MemoryStore::empty()),
            transport: Box::new(FixtureTransport {
                response: success_response(),
                observed: Arc::new(Mutex::new(None)),
            }),
            entropy: Box::new(FixedEntropy),
        };
        service
            .exchange(&exchange_params(), EXTENSION_ID, INSTALL_A)
            .unwrap();

        assert_eq!(
            service.exchange(&exchange_params(), EXTENSION_ID, INSTALL_A),
            Err(PairingFailure::AlreadyPaired)
        );
        let other_extension = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        assert_eq!(
            service.hello(other_extension, INSTALL_A).server_state,
            "unpaired"
        );
        let isolated_status = service.status(other_extension, INSTALL_A).encode();
        assert!(isolated_status.contains("\"state\":\"unpaired\""));
        assert!(!isolated_status.contains(BASE_ORIGIN));
        assert!(service
            .disconnect(other_extension, INSTALL_A)
            .unwrap()
            .encode()
            .contains("\"state\":\"unpaired\""));
        assert_eq!(
            service.hello(EXTENSION_ID, INSTALL_A).server_state,
            "paired"
        );
    }

    #[test]
    fn two_chrome_profiles_pair_and_disconnect_in_independent_slots() {
        let service = PairingService {
            credential_store: Box::new(MemoryStore::empty()),
            transport: Box::new(SequenceTransport {
                responses: Mutex::new(VecDeque::from([
                    success_response_for("bridge-profile-a"),
                    success_response_for("bridge-profile-b"),
                ])),
            }),
            entropy: Box::new(FixedEntropy),
        };

        service
            .exchange(&exchange_params(), EXTENSION_ID, INSTALL_A)
            .unwrap();
        assert_eq!(
            service.hello(EXTENSION_ID, INSTALL_B).server_state,
            "unpaired"
        );
        service
            .exchange(&exchange_params(), EXTENSION_ID, INSTALL_B)
            .unwrap();

        let profile_a = service
            .connector_credential(EXTENSION_ID, INSTALL_A)
            .unwrap()
            .unwrap();
        let profile_b = service
            .connector_credential(EXTENSION_ID, INSTALL_B)
            .unwrap()
            .unwrap();
        assert_eq!(profile_a.bridge_id, "bridge-profile-a");
        assert_eq!(profile_a.install_instance_id, INSTALL_A);
        assert_eq!(profile_b.bridge_id, "bridge-profile-b");
        assert_eq!(profile_b.install_instance_id, INSTALL_B);
        assert_ne!(
            profile_credential_slot(EXTENSION_ID, INSTALL_A).unwrap(),
            profile_credential_slot(EXTENSION_ID, INSTALL_B).unwrap()
        );

        service.disconnect(EXTENSION_ID, INSTALL_A).unwrap();
        assert!(service
            .connector_credential(EXTENSION_ID, INSTALL_A)
            .unwrap()
            .is_none());
        assert_eq!(
            service
                .connector_credential(EXTENSION_ID, INSTALL_B)
                .unwrap()
                .unwrap()
                .bridge_id,
            "bridge-profile-b"
        );
    }

    #[test]
    fn legacy_singleton_record_requires_repair_and_is_never_migrated() {
        let record = CredentialRecord {
            bridge_id: "legacy-bridge".into(),
            server_instance_id: "server-1".into(),
            server_base_origin: BASE_ORIGIN.into(),
            extension_id: EXTENSION_ID.into(),
            install_instance_id: INSTALL_A.into(),
            companion_instance_id: "07070707-0707-4707-8707-070707070707".into(),
            key_generation: 1,
            created_at_ms: 1,
            private_seed: Zeroizing::new([9_u8; PRIVATE_KEY_BYTES]),
        };
        let legacy = legacy_credential_bytes(&record);
        assert!(matches!(
            decode_credential(&legacy),
            Err(PairingFailure::LegacyCredentialPresent)
        ));
        let observed = Arc::new(Mutex::new(None));
        let service = PairingService {
            credential_store: Box::new(MemoryStore::with_legacy(legacy)),
            transport: Box::new(FixtureTransport {
                response: success_response(),
                observed: Arc::clone(&observed),
            }),
            entropy: Box::new(FixedEntropy),
        };

        assert_eq!(
            service.hello(EXTENSION_ID, INSTALL_A).server_state,
            "repair_required"
        );
        assert!(service
            .status(EXTENSION_ID, INSTALL_A)
            .encode()
            .contains("PAIRING_LEGACY_RECORD_PRESENT"));
        assert!(matches!(
            service.exchange(&exchange_params(), EXTENSION_ID, INSTALL_A),
            Err(PairingFailure::LegacyCredentialPresent)
        ));
        assert!(matches!(
            service.disconnect(EXTENSION_ID, INSTALL_A),
            Err(PairingFailure::LegacyCredentialPresent)
        ));
        assert!(observed.lock().unwrap().is_none());
    }
}
