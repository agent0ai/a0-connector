//! Ownership-safe per-user install/update transaction.
//!
//! The crate-private composer mints authority only after verifying the catalog,
//! artifact, platform signature, provenance and offline self-test against the
//! same open payload handle. Catalog-only evidence is never sufficient.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[path = "release_candidate.rs"]
pub(crate) mod candidate;
#[path = "install_lifecycle.rs"]
pub(crate) mod lifecycle;

use sha2::{Digest, Sha256};

use super::{BrowserTarget, InstallOperation};
use crate::json::{self, Value};
use crate::manifest::{generate_exact_manifest, is_exact_extension_origin};
use crate::platform::{architecture, discover_user_paths, Platform, UserPaths};
use crate::registry::{
    discover_stable_browsers, registration_location, BrowserDiscoveryError, BrowserId,
    RegistrationLocation, BROWSERS,
};
use crate::{
    EXIT_INTEGRITY_OR_POLICY, EXIT_NOT_INSTALLED, EXIT_OK, EXIT_RELEASE_UNAVAILABLE,
    NATIVE_HOST_NAME,
};

const MAX_PAYLOAD_BYTES: u64 = 512 * 1024 * 1024;
const MAX_STATE_BYTES: usize = 64 * 1024;
const MAX_JOURNALS: usize = 8;
const LOCK_ATTEMPTS: usize = 40;
const LOCK_WAIT: Duration = Duration::from_millis(25);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InstallSource {
    A0Cli,
    InteractiveInstaller,
}

impl InstallSource {
    const fn as_str(self) -> &'static str {
        match self {
            Self::A0Cli => "a0_cli",
            Self::InteractiveInstaller => "interactive_installer",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InstallTransactionError {
    CandidateInvalid,
    CandidatePlatformMismatch,
    CandidatePayloadChanged,
    ReleaseEvidenceUnavailable,
    InstallRecoveryRequired,
    LocalRetirementRecoveryRequired,
    RepairRecoveryRequired,
    PayloadInvalid,
    DowngradeNotAllowed,
    UserPathsUnavailable,
    UnsupportedPlatform,
    WindowsAdapterUnavailable,
    BrowserDiscoveryUnavailable,
    NoBrowserTargets,
    RegistrationUnavailable,
    RegistrationConflict,
    OwnedStateInvalid,
    OwnedRegistrationChanged,
    ReleaseCollision,
    InstallBusy,
    PrivateInstallRootUnavailable,
    JournalInvalid,
    RecoveryConflict,
    Filesystem,
    EntropyUnavailable,
    #[cfg(test)]
    InjectedFailure,
    #[cfg(test)]
    InjectedCrash,
}

impl InstallTransactionError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::CandidateInvalid => "VERIFIED_INSTALL_CANDIDATE_INVALID",
            Self::CandidatePlatformMismatch => "RELEASE_PLATFORM_MISMATCH",
            Self::CandidatePayloadChanged => "VERIFIED_PAYLOAD_HANDLE_CHANGED",
            Self::ReleaseEvidenceUnavailable => "RELEASE_EVIDENCE_UNAVAILABLE",
            Self::InstallRecoveryRequired => "INSTALL_RECOVERY_REQUIRED",
            Self::LocalRetirementRecoveryRequired => "LOCAL_RETIREMENT_RECOVERY_REQUIRED",
            Self::RepairRecoveryRequired => "REPAIR_RECOVERY_REQUIRED",
            Self::PayloadInvalid => "ARTIFACT_READBACK_FAILED",
            Self::DowngradeNotAllowed => "COMPANION_DOWNGRADE_NOT_ALLOWED",
            Self::UserPathsUnavailable => "USER_ROOT_UNAVAILABLE",
            Self::UnsupportedPlatform => "UNSUPPORTED_PLATFORM",
            Self::WindowsAdapterUnavailable => "WINDOWS_INSTALL_ADAPTER_UNAVAILABLE",
            Self::BrowserDiscoveryUnavailable => "BROWSER_DISCOVERY_NOT_AVAILABLE",
            Self::NoBrowserTargets => "NO_SUPPORTED_BROWSER_FOUND",
            Self::RegistrationUnavailable => "BROWSER_REGISTRATION_UNAVAILABLE",
            Self::RegistrationConflict => "REGISTRATION_CONFLICT",
            Self::OwnedStateInvalid => "INSTALL_STATE_INVALID",
            Self::OwnedRegistrationChanged => "OWNED_REGISTRATION_CHANGED",
            Self::ReleaseCollision => "RELEASE_PATH_CONFLICT",
            Self::InstallBusy => "INSTALL_BUSY",
            Self::PrivateInstallRootUnavailable => "PRIVATE_INSTALL_ROOT_UNAVAILABLE",
            Self::JournalInvalid => "INSTALL_JOURNAL_INVALID",
            Self::RecoveryConflict => "INSTALL_RECOVERY_CONFLICT",
            Self::Filesystem => "INSTALL_FILESYSTEM_ERROR",
            Self::EntropyUnavailable => "INSTALL_ENTROPY_UNAVAILABLE",
            #[cfg(test)]
            Self::InjectedFailure => "TEST_INJECTED_FAILURE",
            #[cfg(test)]
            Self::InjectedCrash => "TEST_INJECTED_CRASH",
        }
    }

    pub const fn exit_code(self) -> u8 {
        match self {
            Self::LocalRetirementRecoveryRequired | Self::RepairRecoveryRequired => 6,
            Self::NoBrowserTargets => EXIT_NOT_INSTALLED,
            Self::UserPathsUnavailable
            | Self::BrowserDiscoveryUnavailable
            | Self::ReleaseEvidenceUnavailable => EXIT_RELEASE_UNAVAILABLE,
            _ => EXIT_INTEGRITY_OR_POLICY,
        }
    }
}

#[derive(Debug, Eq, PartialEq)]
pub struct InstallTransactionResult {
    pub operation: InstallOperation,
    pub active_version: String,
    pub registered_browsers: Vec<BrowserId>,
    pub registration_count: usize,
    pub already_current: bool,
    pub rollback: &'static str,
    pub exit_code: u8,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PayloadIdentity {
    device: u64,
    inode: u64,
    length: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct BoundVerification {
    release: String,
    release_catalog_key_id: String,
    catalog_sha256: String,
    artifact_sha256: String,
    platform: Platform,
    artifact_arch: String,
    origin_set_sha256: String,
    payload: PayloadIdentity,
}

#[allow(dead_code)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CandidateAuthority {
    Production,
    #[cfg(test)]
    TestFixture,
}

/// Opaque authority to mutate the stable per-user installation.
///
/// Only the crate-private release composer can construct production authority.
/// Public callers cannot turn booleans, paths, or catalog metadata into an
/// install candidate. All mandatory checks bind the retained executable handle.
pub struct FullyVerifiedInstallCandidate {
    version: String,
    release_catalog_key_id: String,
    catalog_sha256: String,
    artifact_sha256: String,
    artifact_size: u64,
    platform: Platform,
    artifact_arch: String,
    allowed_origins: Vec<String>,
    payload: File,
    payload_identity: PayloadIdentity,
    catalog_artifact: BoundVerification,
    platform_signature: BoundVerification,
    provenance: BoundVerification,
    offline_self_test: BoundVerification,
    authority: CandidateAuthority,
    // Retain archive/executable staging and their cleanup owner until the
    // transaction finishes. The executable file above duplicates that same
    // read-only file object; no executable pathname is reopened.
    _derived_payload: Option<crate::release_payload::VerifiedExecutablePayload>,
    installed_evidence: Option<candidate::InstalledReleaseEvidence>,
}

impl std::fmt::Debug for FullyVerifiedInstallCandidate {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("FullyVerifiedInstallCandidate")
            .field("version", &self.version)
            .field("platform", &self.platform)
            .field("artifact_arch", &self.artifact_arch)
            .field("artifact_size", &self.artifact_size)
            .finish_non_exhaustive()
    }
}

impl FullyVerifiedInstallCandidate {
    fn validate(&self, platform: Platform, arch: &str) -> Result<(), InstallTransactionError> {
        if !valid_version(&self.version)
            || !valid_identifier(&self.release_catalog_key_id, 128)
            || !valid_sha256(&self.catalog_sha256)
            || !valid_sha256(&self.artifact_sha256)
            || self.artifact_size == 0
            || self.artifact_size > MAX_PAYLOAD_BYTES
            || self.allowed_origins.is_empty()
            || self.allowed_origins.len() > 16
            || self
                .allowed_origins
                .iter()
                .any(|origin| !is_exact_extension_origin(origin))
            || self.allowed_origins.iter().collect::<BTreeSet<_>>().len()
                != self.allowed_origins.len()
        {
            return Err(InstallTransactionError::CandidateInvalid);
        }
        if self.platform != platform || self.artifact_arch != expected_artifact_arch(platform, arch)
        {
            return Err(InstallTransactionError::CandidatePlatformMismatch);
        }
        let expected = BoundVerification {
            release: self.version.clone(),
            release_catalog_key_id: self.release_catalog_key_id.clone(),
            catalog_sha256: self.catalog_sha256.clone(),
            artifact_sha256: self.artifact_sha256.clone(),
            platform: self.platform,
            artifact_arch: self.artifact_arch.clone(),
            origin_set_sha256: origin_set_sha256(&self.allowed_origins),
            payload: self.payload_identity.clone(),
        };
        if self.catalog_artifact != expected
            || self.platform_signature != expected
            || self.provenance != expected
            || self.offline_self_test != expected
        {
            return Err(InstallTransactionError::CandidateInvalid);
        }
        match self.authority {
            CandidateAuthority::Production => {
                let supplied = self
                    .allowed_origins
                    .iter()
                    .map(String::as_str)
                    .collect::<BTreeSet<_>>();
                let compiled = crate::release::PRODUCTION_EXTENSION_ORIGINS
                    .iter()
                    .copied()
                    .collect::<BTreeSet<_>>();
                if !crate::release::release_trust_configured()
                    || self.installed_evidence.is_none()
                    || supplied.len() != self.allowed_origins.len()
                    || supplied != compiled
                {
                    return Err(InstallTransactionError::CandidateInvalid);
                }
            }
            #[cfg(test)]
            CandidateAuthority::TestFixture => {}
        }
        verify_payload_identity(&self.payload, &self.payload_identity)
    }

    #[cfg(test)]
    fn test_fixture(
        mut payload: File,
        version: &str,
        platform: Platform,
        arch: &str,
    ) -> Result<Self, InstallTransactionError> {
        let identity = payload_identity(&payload)?;
        payload
            .seek(SeekFrom::Start(0))
            .map_err(|_| InstallTransactionError::Filesystem)?;
        let (digest, size) = hash_reader(&mut payload, MAX_PAYLOAD_BYTES)?;
        payload
            .seek(SeekFrom::Start(0))
            .map_err(|_| InstallTransactionError::Filesystem)?;
        let bound = BoundVerification {
            release: version.to_owned(),
            release_catalog_key_id: "fixture-release-root".to_owned(),
            catalog_sha256: "1".repeat(64),
            artifact_sha256: digest.clone(),
            platform,
            artifact_arch: expected_artifact_arch(platform, arch).to_owned(),
            origin_set_sha256: origin_set_sha256(&[
                "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/".to_owned(),
            ]),
            payload: identity.clone(),
        };
        Ok(Self {
            version: version.to_owned(),
            release_catalog_key_id: "fixture-release-root".to_owned(),
            catalog_sha256: "1".repeat(64),
            artifact_sha256: digest,
            artifact_size: size,
            platform,
            artifact_arch: expected_artifact_arch(platform, arch).to_owned(),
            allowed_origins: vec!["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/".to_owned()],
            payload,
            payload_identity: identity,
            catalog_artifact: bound.clone(),
            platform_signature: bound.clone(),
            provenance: bound.clone(),
            offline_self_test: bound,
            authority: CandidateAuthority::TestFixture,
            _derived_payload: None,
            installed_evidence: None,
        })
    }
}

pub fn install_verified_candidate(
    operation: InstallOperation,
    targets: Vec<BrowserTarget>,
    source: InstallSource,
    candidate: FullyVerifiedInstallCandidate,
) -> Result<InstallTransactionResult, InstallTransactionError> {
    let platform = Platform::current();
    if platform == Platform::Windows {
        return Err(InstallTransactionError::WindowsAdapterUnavailable);
    }
    if platform == Platform::Unsupported {
        return Err(InstallTransactionError::UnsupportedPlatform);
    }
    let paths = discover_user_paths().map_err(|_| InstallTransactionError::UserPathsUnavailable)?;
    execute_at(
        operation,
        targets,
        source,
        candidate,
        platform,
        architecture(),
        paths,
        now_ms()?,
        Fault::None,
    )
}

/// Concrete stable acquisition boundary used by the native installer command.
/// All network locations and signer roots are compiled; no stable installation
/// mutation occurs until the complete retained candidate has passed every gate.
#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn acquire_and_install(
    operation: InstallOperation,
    targets: Vec<BrowserTarget>,
) -> Result<InstallTransactionResult, InstallTransactionError> {
    let paths = discover_user_paths().map_err(|_| InstallTransactionError::UserPathsUnavailable)?;
    let state = load_state(&paths.install_root.join("install-state.json"))?;
    let floor = state
        .as_ref()
        .map(|state| state.active_version.as_str())
        .unwrap_or(crate::release::catalog::MINIMUM_SECURE_COMPANION);
    #[cfg(target_os = "macos")]
    let acquire = candidate::acquire_macos_production_candidate;
    #[cfg(target_os = "linux")]
    let acquire = candidate::acquire_linux_production_candidate;
    #[cfg(target_os = "macos")]
    let staging_parent = std::env::temp_dir();
    #[cfg(target_os = "linux")]
    let staging_parent = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .ok_or(InstallTransactionError::UserPathsUnavailable)?;
    let candidate =
        acquire(crate::COMPANION_VERSION, floor, &staging_parent).map_err(|error| match error {
            candidate::CompositionError::ReleaseTrustUnavailable => {
                InstallTransactionError::ReleaseEvidenceUnavailable
            }
            _ => InstallTransactionError::CandidateInvalid,
        })?;
    install_verified_candidate(
        operation,
        targets,
        InstallSource::InteractiveInstaller,
        candidate,
    )
}

pub(crate) struct VerifiedInstalledStatus {
    pub registered_browser_count: usize,
    registration_count: usize,
}

/// Read-only stable status. Never creates a lock/root, repairs a journal,
/// executes the installed program, downloads metadata, or accepts self-report.
pub(crate) fn verify_installed_status(
    paths: &UserPaths,
) -> Result<Option<VerifiedInstalledStatus>, InstallTransactionError> {
    let state_path = paths.install_root.join("install-state.json");
    match fs::symlink_metadata(&state_path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(InstallTransactionError::OwnedStateInvalid),
        Ok(_) => {}
    }
    if cfg!(feature = "local-development") || !crate::release::release_trust_configured() {
        return Err(InstallTransactionError::ReleaseEvidenceUnavailable);
    }
    let platform = Platform::current();
    if !matches!(platform, Platform::Macos | Platform::Linux) {
        return Err(InstallTransactionError::WindowsAdapterUnavailable);
    }
    verify_installed_at(
        paths,
        platform,
        expected_artifact_arch(platform, architecture()),
        crate::release::PRODUCTION_EXTENSION_ORIGINS,
    )
    .map(Some)
}

fn verify_installed_at(
    paths: &UserPaths,
    platform: Platform,
    arch: &str,
    origins: &[&str],
) -> Result<VerifiedInstalledStatus, InstallTransactionError> {
    inspect_installed_at(paths, platform, arch, origins, None, &[])
}

fn inspect_installed_at(
    paths: &UserPaths,
    platform: Platform,
    arch: &str,
    origins: &[&str],
    operation: Option<lifecycle::Operation>,
    targets: &[BrowserTarget],
) -> Result<VerifiedInstalledStatus, InstallTransactionError> {
    verify_private_directory(&paths.install_root)?;
    let lock_file = open_owned_readonly(&paths.install_root.join("install.lock"), true, false)?;
    if !try_flock_exclusive(&lock_file)? {
        return Err(InstallTransactionError::InstallBusy);
    }
    let _lock = InstallLock { file: lock_file };
    let transactions = paths.install_root.join("transactions");
    verify_private_directory(&transactions)?;
    if fs::read_dir(&transactions)
        .map_err(|_| InstallTransactionError::Filesystem)?
        .next()
        .is_some()
    {
        return Err(InstallTransactionError::InstallRecoveryRequired);
    }
    let state_path = paths.install_root.join("install-state.json");
    let mut state_file = open_owned_readonly(&state_path, true, false)?;
    let state_identity = payload_identity(&state_file)?;
    let mut bytes = Vec::new();
    (&mut state_file)
        .take(MAX_STATE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    if bytes.len() > MAX_STATE_BYTES {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let state =
        parse_state(&json::parse(&bytes).map_err(|_| InstallTransactionError::OwnedStateInvalid)?)?;
    if !targets.is_empty() && !automatic_target_selection(targets)? {
        let requested = targets
            .iter()
            .filter_map(|target| match target {
                BrowserTarget::Explicit(browser) => Some(*browser),
                _ => None,
            })
            .collect::<BTreeSet<_>>();
        if requested
            != state
                .registered_browsers
                .iter()
                .copied()
                .collect::<BTreeSet<_>>()
        {
            return Err(InstallTransactionError::RegistrationConflict);
        }
    }
    if !valid_version(&state.active_version)
        || version_tuple(&state.active_version)
            < version_tuple(crate::release::catalog::MINIMUM_SECURE_COMPANION)
        || state.platform != platform
        || state.artifact_arch != arch
        || origins.is_empty()
    {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let binary = release_binary_path(
        &paths.install_root,
        &state.active_version,
        platform,
        &state.artifact_arch,
    );
    for directory in [
        paths.install_root.join("releases"),
        binary.parent().unwrap().parent().unwrap().to_owned(),
        binary.parent().unwrap().to_owned(),
    ] {
        verify_private_directory(&directory)?;
    }
    let repair = operation == Some(lifecycle::Operation::Repair);
    let mut executable = open_owned_for_inspection(&binary, true, true, repair)?;
    let retained = payload_identity(&executable)?;
    let (digest, size) = hash_reader(&mut executable, MAX_PAYLOAD_BYTES)?;
    if digest != state.active_artifact_sha256 || size != state.active_artifact_size {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let directory = binary
        .parent()
        .ok_or(InstallTransactionError::OwnedStateInvalid)?;
    let catalog = read_owned_evidence(&directory.join("release-catalog.json"), 128 * 1024)?;
    let catalog_signature = read_owned_evidence(&directory.join("release-catalog.sig"), 64)?;
    let provenance = read_owned_evidence(&directory.join("build-provenance.json"), 16 * 1024)?;
    let provenance_signature = read_owned_evidence(&directory.join("build-provenance.sig"), 64)?;
    candidate::verify_installed_evidence(
        &catalog,
        &catalog_signature,
        &provenance,
        &provenance_signature,
        &state.active_version,
        &state.release_catalog_key_id,
        platform,
        arch,
        &digest,
        size,
    )
    .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    verify_payload_identity(
        &open_owned_for_inspection(&binary, true, true, repair)?,
        &retained,
    )?;
    let manifest = generate_exact_manifest(&binary, origins)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    let manifest_digest = sha256(manifest.as_bytes());
    let mut registrations = Vec::new();
    for (path, recorded_digest) in owned_registration_paths(Some(&state), platform, paths)? {
        let base = if platform == Platform::Macos {
            paths.home_root.as_ref()
        } else {
            paths.config_root.as_ref()
        }
        .ok_or(InstallTransactionError::OwnedStateInvalid)?;
        if recorded_digest != manifest_digest {
            return Err(InstallTransactionError::OwnedRegistrationChanged);
        }
        let parent = path
            .parent()
            .ok_or(InstallTransactionError::OwnedStateInvalid)?;
        let missing_parent = repair
            && matches!(fs::symlink_metadata(parent), Err(error) if error.kind() == io::ErrorKind::NotFound);
        verify_registration_parents(
            if missing_parent {
                parent
                    .parent()
                    .ok_or(InstallTransactionError::OwnedStateInvalid)?
            } else {
                parent
            },
            base,
        )?;
        if matches!(
            operation,
            Some(lifecycle::Operation::Repair | lifecycle::Operation::Uninstall)
        ) && matches!(fs::symlink_metadata(&path), Err(error) if error.kind() == io::ErrorKind::NotFound)
        {
            registrations.push(lifecycle::Registration {
                path,
                file: None,
                missing_parent,
            });
            continue;
        }
        let mut file = open_owned_for_inspection(&path, false, false, repair)?;
        let identity = payload_identity(&file)?;
        let (digest, _) = hash_reader(&mut file, MAX_STATE_BYTES as u64)?;
        if digest != manifest_digest || digest != recorded_digest {
            return Err(InstallTransactionError::OwnedRegistrationChanged);
        }
        verify_payload_identity(
            &open_owned_for_inspection(&path, false, false, repair)?,
            &identity,
        )?;
        registrations.push(lifecycle::Registration {
            path,
            file: Some(file),
            missing_parent,
        });
    }
    verify_payload_identity(
        &open_owned_readonly(&state_path, true, false)?,
        &state_identity,
    )?;
    if read_bounded(&state_path, MAX_STATE_BYTES)? != bytes {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    if let Some(operation) = operation {
        lifecycle::apply_verified(
            operation,
            paths,
            &binary,
            &executable,
            &state_file,
            &bytes,
            &manifest,
            &registrations,
        )?;
    }
    Ok(VerifiedInstalledStatus {
        registered_browser_count: state.registered_browsers.len(),
        registration_count: if operation == Some(lifecycle::Operation::Uninstall) {
            registrations
                .iter()
                .filter(|registration| registration.file.is_some())
                .count()
        } else {
            registrations.len()
        },
    })
}

fn read_owned_evidence(path: &Path, limit: usize) -> Result<Vec<u8>, InstallTransactionError> {
    let mut file = open_owned_readonly(path, true, false)?;
    let identity = payload_identity(&file)?;
    let mut bytes = Vec::new();
    (&mut file)
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    if bytes.is_empty() || bytes.len() > limit {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    verify_payload_identity(&file, &identity)?;
    verify_payload_identity(&open_owned_readonly(path, true, false)?, &identity)?;
    Ok(bytes)
}

#[cfg(unix)]
fn verify_registration_parents(path: &Path, base: &Path) -> Result<(), InstallTransactionError> {
    use std::os::unix::fs::MetadataExt;
    unsafe extern "C" {
        fn getuid() -> u32;
    }
    let relative = path
        .strip_prefix(base)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    let mut current = base.to_owned();
    let components = std::iter::once(None).chain(relative.components().map(Some));
    for component in components {
        if let Some(component) = component {
            if !matches!(component, std::path::Component::Normal(_)) {
                return Err(InstallTransactionError::OwnedStateInvalid);
            }
            current.push(component);
        }
        let metadata = fs::symlink_metadata(&current)
            .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
        if !metadata.is_dir()
            || metadata.file_type().is_symlink()
            || metadata.uid() != unsafe { getuid() }
            || metadata.mode() & 0o022 != 0
        {
            return Err(InstallTransactionError::OwnedStateInvalid);
        }
    }
    Ok(())
}

#[cfg(not(unix))]
fn verify_registration_parents(_path: &Path, _base: &Path) -> Result<(), InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

#[cfg(unix)]
fn verify_private_directory(path: &Path) -> Result<(), InstallTransactionError> {
    use std::os::unix::fs::MetadataExt;
    unsafe extern "C" {
        fn getuid() -> u32;
    }
    let metadata =
        fs::symlink_metadata(path).map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { getuid() }
        || metadata.mode() & 0o077 != 0
    {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    Ok(())
}

#[cfg(unix)]
fn open_owned_readonly(
    path: &Path,
    private: bool,
    executable: bool,
) -> Result<File, InstallTransactionError> {
    open_owned_for_inspection(path, private, executable, false)
}

#[cfg(unix)]
fn open_owned_for_inspection(
    path: &Path,
    private: bool,
    executable: bool,
    repair_permissions: bool,
) -> Result<File, InstallTransactionError> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    unsafe extern "C" {
        fn getuid() -> u32;
    }
    #[cfg(target_os = "macos")]
    const O_NOFOLLOW: i32 = 0x100;
    #[cfg(not(target_os = "macos"))]
    const O_NOFOLLOW: i32 = 0x20000;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(path)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    let metadata = file
        .metadata()
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    let path_metadata =
        fs::symlink_metadata(path).map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { getuid() }
        || metadata.nlink() != 1
        || (!repair_permissions && metadata.mode() & if private { 0o077 } else { 0o022 } != 0)
        || (!repair_permissions && executable && metadata.mode() & 0o100 == 0)
        || metadata.dev() != path_metadata.dev()
        || metadata.ino() != path_metadata.ino()
        || path_metadata.file_type().is_symlink()
    {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    Ok(file)
}

#[cfg(not(unix))]
fn verify_private_directory(_path: &Path) -> Result<(), InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

#[cfg(not(unix))]
fn open_owned_readonly(
    _path: &Path,
    _private: bool,
    _executable: bool,
) -> Result<File, InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

#[cfg(not(unix))]
fn open_owned_for_inspection(
    _path: &Path,
    _private: bool,
    _executable: bool,
    _repair: bool,
) -> Result<File, InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Fault {
    None,
    #[cfg(test)]
    FailAfterFirstRegistration,
    #[cfg(test)]
    CrashAfterFirstRegistration,
    #[cfg(test)]
    CrashAfterStateCommit,
}

#[allow(clippy::too_many_arguments)]
fn execute_at(
    operation: InstallOperation,
    targets: Vec<BrowserTarget>,
    source: InstallSource,
    candidate: FullyVerifiedInstallCandidate,
    platform: Platform,
    arch: &str,
    paths: UserPaths,
    installed_at_ms: u64,
    fault: Fault,
) -> Result<InstallTransactionResult, InstallTransactionError> {
    execute_at_with_discovery(
        operation,
        targets,
        source,
        candidate,
        platform,
        arch,
        paths,
        installed_at_ms,
        fault,
        discover_stable_browsers,
    )
}

#[allow(clippy::too_many_arguments)]
fn execute_at_with_discovery<F>(
    operation: InstallOperation,
    targets: Vec<BrowserTarget>,
    source: InstallSource,
    mut candidate: FullyVerifiedInstallCandidate,
    platform: Platform,
    arch: &str,
    paths: UserPaths,
    installed_at_ms: u64,
    fault: Fault,
    discover: F,
) -> Result<InstallTransactionResult, InstallTransactionError>
where
    F: FnOnce(Platform, &UserPaths) -> Result<Vec<BrowserId>, BrowserDiscoveryError>,
{
    #[cfg(not(test))]
    let _ = fault;
    if platform == Platform::Windows {
        return Err(InstallTransactionError::WindowsAdapterUnavailable);
    }
    if !matches!(platform, Platform::Macos | Platform::Linux) {
        return Err(InstallTransactionError::UnsupportedPlatform);
    }
    candidate.validate(platform, arch)?;
    validate_user_paths(&paths, platform)?;
    let automatic = automatic_target_selection(&targets)?;
    let discovered = if automatic {
        discover(platform, &paths).map_err(map_browser_discovery_error)?
    } else {
        Vec::new()
    };
    if automatic && operation == InstallOperation::Install && discovered.is_empty() {
        return Err(InstallTransactionError::NoBrowserTargets);
    }
    ensure_private_root(&paths.install_root)?;
    let transactions = paths.install_root.join("transactions");
    let releases = paths.install_root.join("releases");
    ensure_private_dir(&transactions)?;
    ensure_private_dir(&releases)?;
    let _lock = InstallLock::acquire(&paths.install_root.join("install.lock"))?;
    recover_interrupted(&paths, platform, &candidate.artifact_arch)?;

    let old_state = load_state(&paths.install_root.join("install-state.json"))?;
    if old_state.as_ref().is_some_and(|state| {
        state.platform != platform || state.artifact_arch != candidate.artifact_arch
    }) {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    if old_state.as_ref().is_some_and(|state| {
        version_tuple(&candidate.version) < version_tuple(&state.active_version)
    }) {
        return Err(InstallTransactionError::DowngradeNotAllowed);
    }
    let reusable_owned = if automatic && operation == InstallOperation::Update {
        old_state
            .as_ref()
            .map(|state| {
                validate_owned_state_for_auto_update(state, platform, &paths)?;
                Ok(state.registered_browsers.as_slice())
            })
            .transpose()?
    } else {
        None
    };
    let browsers = resolve_targets(&targets, &discovered, reusable_owned)?;
    let registrations = resolve_registration_paths(&browsers, platform, &paths)?;
    let manifest = generate_exact_manifest(
        &release_binary_path(
            &paths.install_root,
            &candidate.version,
            platform,
            &candidate.artifact_arch,
        ),
        &candidate.allowed_origins,
    )
    .map_err(|_| InstallTransactionError::CandidateInvalid)?;
    let manifest_digest = sha256(manifest.as_bytes());
    validate_current_ownership(old_state.as_ref(), platform, &paths, &registrations)?;

    if let Some(state) = old_state.as_ref() {
        if state.matches_candidate(&candidate, &browsers, &registrations, &manifest_digest)
            && verify_healthy_install(state, platform, &paths, &candidate)?
        {
            return Ok(InstallTransactionResult {
                operation,
                active_version: candidate.version,
                registered_browsers: browsers,
                registration_count: registrations.len(),
                already_current: true,
                rollback: "not_needed",
                exit_code: EXIT_OK,
            });
        }
    }

    let transaction_id = random_id()?;
    let journal_path = transactions.join(format!("{transaction_id}.json"));
    let work_dir = transactions.join(format!("{transaction_id}.d"));
    let final_release_dir = release_dir(
        &paths.install_root,
        &candidate.version,
        platform,
        &candidate.artifact_arch,
    );
    let final_binary = final_release_dir.join(binary_name(platform));
    let mut journal = Journal {
        transaction_id: transaction_id.clone(),
        release_dir: final_release_dir.clone(),
        binary_path: final_binary.clone(),
        artifact_sha256: candidate.artifact_sha256.clone(),
        release_was_created: false,
        registrations: Vec::new(),
    };
    write_journal(&journal_path, &journal)?;
    ensure_new_private_dir(&work_dir)?;

    let transaction_result = (|| {
        stage_release(
            &mut candidate,
            &work_dir,
            &final_release_dir,
            old_state.as_ref(),
            &mut journal,
            &journal_path,
        )?;

        let desired = registrations
            .iter()
            .map(|(path, _)| (path.clone(), Some(manifest.as_bytes().to_vec())))
            .collect::<BTreeMap<_, _>>();
        let old = owned_registration_paths(old_state.as_ref(), platform, &paths)?;
        let mut mutations = desired;
        for path in old.keys() {
            mutations.entry(path.clone()).or_insert(None);
        }

        for (index, (path, desired_bytes)) in mutations.into_iter().enumerate() {
            mutate_registration(
                &path,
                desired_bytes.as_deref(),
                old.get(&path).map(String::as_str),
                &work_dir,
                &mut journal,
                &journal_path,
            )?;
            #[cfg(test)]
            if index == 0 {
                match fault {
                    Fault::FailAfterFirstRegistration => {
                        return Err(InstallTransactionError::InjectedFailure)
                    }
                    Fault::CrashAfterFirstRegistration => {
                        return Err(InstallTransactionError::InjectedCrash)
                    }
                    Fault::None | Fault::CrashAfterStateCommit => {}
                }
            }
            #[cfg(not(test))]
            let _ = index;
        }

        verify_binary(
            &final_binary,
            &candidate.artifact_sha256,
            candidate.artifact_size,
        )?;
        for path in registrations.keys() {
            if hash_regular_file(path, MAX_STATE_BYTES as u64)?.as_deref()
                != Some(manifest_digest.as_str())
            {
                return Err(InstallTransactionError::OwnedRegistrationChanged);
            }
        }

        let install_id = old_state
            .as_ref()
            .map(|state| state.install_id.clone())
            .unwrap_or(random_id()?);
        let previous = old_state.as_ref().and_then(|state| {
            (state.active_version != candidate.version).then(|| PreviousRelease {
                version: state.active_version.clone(),
                sha256: state.active_artifact_sha256.clone(),
            })
        });
        let state = InstallState {
            install_id,
            active_version: candidate.version.clone(),
            active_artifact_sha256: candidate.artifact_sha256.clone(),
            active_artifact_size: candidate.artifact_size,
            platform,
            artifact_arch: candidate.artifact_arch.clone(),
            previous,
            installed_at_ms,
            source,
            registered_browsers: browsers.clone(),
            release_catalog_key_id: candidate.release_catalog_key_id.clone(),
            registrations: registrations
                .keys()
                .map(|path| OwnedRegistration {
                    path_sha256: path_sha256(path),
                    manifest_sha256: manifest_digest.clone(),
                })
                .collect(),
            last_transaction_id: transaction_id.clone(),
        };
        let state_path = paths.install_root.join("install-state.json");
        let write_result = atomic_write(&state_path, state.encode().as_bytes(), 0o600);
        let loaded = load_state(&state_path)?.ok_or(InstallTransactionError::OwnedStateInvalid)?;
        if loaded != state {
            return Err(InstallTransactionError::OwnedStateInvalid);
        }
        // A directory fsync error after rename is an uncertain durability
        // signal, not evidence that the state rename did not happen. Exact
        // readback proves the requested state for this process; the retained
        // journal lets the next run cleanly classify it as committed.
        let _ = write_result;
        #[cfg(test)]
        if fault == Fault::CrashAfterStateCommit {
            return Err(InstallTransactionError::InjectedCrash);
        }
        Ok(state)
    })();

    match transaction_result {
        Ok(state) => {
            let cleanup_pending = remove_transaction_files(&journal_path, &work_dir).is_err()
                || prune_superseded_release(
                    &paths.install_root,
                    platform,
                    &candidate.artifact_arch,
                    old_state.as_ref(),
                    &state,
                )
                .is_err();
            Ok(InstallTransactionResult {
                operation,
                active_version: state.active_version,
                registered_browsers: browsers,
                registration_count: registrations.len(),
                already_current: false,
                rollback: if cleanup_pending {
                    "cleanup_pending"
                } else {
                    "not_needed"
                },
                exit_code: if cleanup_pending { 6 } else { EXIT_OK },
            })
        }
        Err(error) => {
            #[cfg(test)]
            if error == InstallTransactionError::InjectedCrash {
                return Err(error);
            }
            rollback(&paths, platform, &journal, &journal_path, &work_dir)?;
            Err(error)
        }
    }
}

fn automatic_target_selection(targets: &[BrowserTarget]) -> Result<bool, InstallTransactionError> {
    let automatic = targets
        .iter()
        .filter(|target| matches!(target, BrowserTarget::Auto))
        .count();
    if automatic == 0 {
        return Ok(false);
    }
    if automatic == 1 && targets.len() == 1 {
        Ok(true)
    } else {
        Err(InstallTransactionError::BrowserDiscoveryUnavailable)
    }
}

fn map_browser_discovery_error(_error: BrowserDiscoveryError) -> InstallTransactionError {
    InstallTransactionError::BrowserDiscoveryUnavailable
}

fn resolve_targets(
    targets: &[BrowserTarget],
    discovered: &[BrowserId],
    reusable_owned: Option<&[BrowserId]>,
) -> Result<Vec<BrowserId>, InstallTransactionError> {
    let mut browsers = if automatic_target_selection(targets)? {
        discovered.iter().copied().collect::<BTreeSet<_>>()
    } else {
        targets
            .iter()
            .filter_map(|target| match target {
                BrowserTarget::Explicit(browser) => Some(*browser),
                BrowserTarget::Auto => None,
            })
            .collect::<BTreeSet<_>>()
    };
    if let Some(owned) = reusable_owned {
        browsers.extend(owned.iter().copied());
    }
    let browsers = browsers.into_iter().collect::<Vec<_>>();
    if browsers.is_empty() {
        return Err(InstallTransactionError::NoBrowserTargets);
    }
    Ok(browsers)
}

fn validate_owned_state_for_auto_update(
    state: &InstallState,
    platform: Platform,
    paths: &UserPaths,
) -> Result<(), InstallTransactionError> {
    let binary = release_binary_path(
        &paths.install_root,
        &state.active_version,
        platform,
        &state.artifact_arch,
    );
    verify_binary(
        &binary,
        &state.active_artifact_sha256,
        state.active_artifact_size,
    )
    .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    for (path, expected) in owned_registration_paths(Some(state), platform, paths)? {
        match hash_regular_file(&path, MAX_STATE_BYTES as u64)? {
            Some(current) if current == expected => {}
            _ => return Err(InstallTransactionError::OwnedRegistrationChanged),
        }
    }
    Ok(())
}

fn resolve_registration_paths(
    browsers: &[BrowserId],
    platform: Platform,
    paths: &UserPaths,
) -> Result<BTreeMap<PathBuf, BTreeSet<BrowserId>>, InstallTransactionError> {
    let mut result = BTreeMap::<PathBuf, BTreeSet<BrowserId>>::new();
    for browser in browsers {
        let Some(RegistrationLocation::ManifestFiles(files)) =
            registration_location(*browser, platform, paths)
        else {
            return Err(InstallTransactionError::RegistrationUnavailable);
        };
        for path in files {
            let expected_file_name = format!("{NATIVE_HOST_NAME}.json");
            if !path.is_absolute()
                || path.file_name().and_then(|value| value.to_str())
                    != Some(expected_file_name.as_str())
            {
                return Err(InstallTransactionError::RegistrationUnavailable);
            }
            result.entry(path).or_default().insert(*browser);
        }
    }
    Ok(result)
}

fn all_registration_paths(
    platform: Platform,
    paths: &UserPaths,
) -> Result<BTreeSet<PathBuf>, InstallTransactionError> {
    Ok(resolve_registration_paths(
        &BROWSERS
            .iter()
            .map(|browser| browser.id)
            .collect::<Vec<_>>(),
        platform,
        paths,
    )?
    .into_keys()
    .collect())
}

fn validate_current_ownership(
    state: Option<&InstallState>,
    platform: Platform,
    paths: &UserPaths,
    desired: &BTreeMap<PathBuf, BTreeSet<BrowserId>>,
) -> Result<(), InstallTransactionError> {
    let owned = owned_registration_paths(state, platform, paths)?;
    let mut candidates = desired.keys().cloned().collect::<BTreeSet<_>>();
    candidates.extend(owned.keys().cloned());
    for path in candidates {
        let current = hash_regular_file(&path, MAX_STATE_BYTES as u64)?;
        match (current, owned.get(&path)) {
            (None, _) => {}
            (Some(current), Some(expected)) if &current == expected => {}
            (Some(_), Some(_)) => return Err(InstallTransactionError::OwnedRegistrationChanged),
            (Some(_), None) => return Err(InstallTransactionError::RegistrationConflict),
        }
    }
    Ok(())
}

fn owned_registration_paths(
    state: Option<&InstallState>,
    platform: Platform,
    paths: &UserPaths,
) -> Result<BTreeMap<PathBuf, String>, InstallTransactionError> {
    let Some(state) = state else {
        return Ok(BTreeMap::new());
    };
    let derived = resolve_registration_paths(&state.registered_browsers, platform, paths)?;
    if derived.len() != state.registrations.len() {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let by_path_digest = state
        .registrations
        .iter()
        .map(|record| (record.path_sha256.as_str(), record.manifest_sha256.as_str()))
        .collect::<BTreeMap<_, _>>();
    if by_path_digest.len() != state.registrations.len() {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let mut owned = BTreeMap::new();
    for path in derived.keys() {
        let digest = path_sha256(path);
        let expected = by_path_digest
            .get(digest.as_str())
            .ok_or(InstallTransactionError::OwnedStateInvalid)?;
        owned.insert(path.clone(), (*expected).to_owned());
    }
    Ok(owned)
}

fn stage_release(
    candidate: &mut FullyVerifiedInstallCandidate,
    work_dir: &Path,
    final_release_dir: &Path,
    old_state: Option<&InstallState>,
    journal: &mut Journal,
    journal_path: &Path,
) -> Result<(), InstallTransactionError> {
    let final_binary = final_release_dir.join(binary_name(candidate.platform));
    if final_release_dir.exists() {
        let owned = old_state.is_some_and(|state| {
            (state.active_version == candidate.version
                && state.active_artifact_sha256 == candidate.artifact_sha256)
                || state.previous.as_ref().is_some_and(|previous| {
                    previous.version == candidate.version
                        && previous.sha256 == candidate.artifact_sha256
                })
        });
        if !owned {
            return Err(InstallTransactionError::ReleaseCollision);
        }
        verify_binary(
            &final_binary,
            &candidate.artifact_sha256,
            candidate.artifact_size,
        )?;
        if let Some(evidence) = &candidate.installed_evidence {
            for (name, expected) in evidence.files() {
                if read_owned_evidence(&final_release_dir.join(name), expected.len())? != expected {
                    return Err(InstallTransactionError::ReleaseCollision);
                }
            }
        }
        return Ok(());
    }

    let staged_dir = work_dir.join("release");
    ensure_new_private_dir(&staged_dir)?;
    let staged_binary = staged_dir.join(binary_name(candidate.platform));
    copy_verified_payload(candidate, &staged_binary)?;
    if let Some(evidence) = &candidate.installed_evidence {
        for (name, bytes) in evidence.files() {
            atomic_write(&staged_dir.join(name), bytes, 0o600)?;
            if read_owned_evidence(&staged_dir.join(name), bytes.len())? != bytes {
                return Err(InstallTransactionError::OwnedStateInvalid);
            }
        }
    }
    atomic_write(
        &staged_dir.join("install-owner-v1"),
        json::object(&[
            ("transaction_id", json::quote(&journal.transaction_id)),
            ("artifact_sha256", json::quote(&candidate.artifact_sha256)),
        ])
        .as_bytes(),
        0o600,
    )?;
    verify_binary(
        &staged_binary,
        &candidate.artifact_sha256,
        candidate.artifact_size,
    )?;
    journal.release_was_created = true;
    write_journal(journal_path, journal)?;
    let parent = final_release_dir
        .parent()
        .ok_or(InstallTransactionError::Filesystem)?;
    ensure_private_dir(parent)?;
    fs::rename(&staged_dir, final_release_dir).map_err(|_| InstallTransactionError::Filesystem)?;
    sync_dir(parent)?;
    verify_binary(
        &final_binary,
        &candidate.artifact_sha256,
        candidate.artifact_size,
    )
}

fn copy_verified_payload(
    candidate: &mut FullyVerifiedInstallCandidate,
    destination: &Path,
) -> Result<(), InstallTransactionError> {
    verify_payload_identity(&candidate.payload, &candidate.payload_identity)?;
    candidate
        .payload
        .seek(SeekFrom::Start(0))
        .map_err(|_| InstallTransactionError::CandidatePayloadChanged)?;
    let mut output = create_new_file(destination, 0o500)?;
    let mut hasher = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; 32 * 1024];
    loop {
        let read = candidate
            .payload
            .read(&mut buffer)
            .map_err(|_| InstallTransactionError::CandidatePayloadChanged)?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or(InstallTransactionError::PayloadInvalid)?;
        if total > candidate.artifact_size || total > MAX_PAYLOAD_BYTES {
            return Err(InstallTransactionError::PayloadInvalid);
        }
        hasher.update(&buffer[..read]);
        output
            .write_all(&buffer[..read])
            .map_err(|_| InstallTransactionError::Filesystem)?;
    }
    if total != candidate.artifact_size || hex(&hasher.finalize()) != candidate.artifact_sha256 {
        return Err(InstallTransactionError::PayloadInvalid);
    }
    output
        .sync_all()
        .map_err(|_| InstallTransactionError::Filesystem)?;
    set_mode(destination, 0o500)?;
    verify_payload_identity(&candidate.payload, &candidate.payload_identity)
}

fn mutate_registration(
    target: &Path,
    desired: Option<&[u8]>,
    expected_prior_digest: Option<&str>,
    work_dir: &Path,
    journal: &mut Journal,
    journal_path: &Path,
) -> Result<(), InstallTransactionError> {
    let parent_created = registration_parent_will_be_created(target, desired.is_some())?;
    let current = hash_regular_file(target, MAX_STATE_BYTES as u64)?;
    match (current.as_deref(), expected_prior_digest) {
        (None, None) => {}
        (Some(current), Some(expected)) if current == expected => {}
        (Some(_), None) => return Err(InstallTransactionError::RegistrationConflict),
        _ => return Err(InstallTransactionError::OwnedRegistrationChanged),
    }
    let backup = if current.is_some() {
        let path = work_dir.join(format!(
            "registration-{}.backup",
            journal.registrations.len()
        ));
        copy_regular_file(target, &path, MAX_STATE_BYTES as u64, 0o600)?;
        if hash_regular_file(&path, MAX_STATE_BYTES as u64)? != current {
            return Err(InstallTransactionError::Filesystem);
        }
        Some(path)
    } else {
        None
    };
    let new_sha256 = desired.map(sha256);
    journal.registrations.push(JournalRegistration {
        target: target.to_path_buf(),
        prior_sha256: current,
        new_sha256,
        backup,
        parent_created,
    });
    write_journal(journal_path, journal)?;

    if parent_created {
        create_registration_parent(target)?;
    }

    match desired {
        Some(bytes) => atomic_write(target, bytes, 0o600),
        None => match fs::remove_file(target) {
            Ok(()) => sync_dir(target.parent().ok_or(InstallTransactionError::Filesystem)?),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(InstallTransactionError::Filesystem),
        },
    }
}

fn verify_healthy_install(
    state: &InstallState,
    platform: Platform,
    paths: &UserPaths,
    candidate: &FullyVerifiedInstallCandidate,
) -> Result<bool, InstallTransactionError> {
    let binary = release_binary_path(
        &paths.install_root,
        &state.active_version,
        platform,
        &candidate.artifact_arch,
    );
    if verify_binary(
        &binary,
        &state.active_artifact_sha256,
        state.active_artifact_size,
    )
    .is_err()
    {
        return Ok(false);
    }
    for (path, digest) in owned_registration_paths(Some(state), platform, paths)? {
        if hash_regular_file(&path, MAX_STATE_BYTES as u64)?.as_deref() != Some(digest.as_str()) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn recover_interrupted(
    paths: &UserPaths,
    platform: Platform,
    artifact_arch: &str,
) -> Result<(), InstallTransactionError> {
    let transactions = paths.install_root.join("transactions");
    let mut journals = fs::read_dir(&transactions)
        .map_err(|_| InstallTransactionError::Filesystem)?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
        .collect::<Vec<_>>();
    journals.sort();
    if journals.len() > MAX_JOURNALS {
        return Err(InstallTransactionError::JournalInvalid);
    }
    let state = load_state(&paths.install_root.join("install-state.json"))?;
    for journal_path in journals {
        let journal = load_journal(&journal_path)?;
        let expected_name = format!("{}.json", journal.transaction_id);
        if journal_path.file_name().and_then(|value| value.to_str()) != Some(&expected_name) {
            return Err(InstallTransactionError::JournalInvalid);
        }
        let work_dir = transactions.join(format!("{}.d", journal.transaction_id));
        if state
            .as_ref()
            .is_some_and(|state| state.last_transaction_id == journal.transaction_id)
        {
            let state = state
                .as_ref()
                .ok_or(InstallTransactionError::RecoveryConflict)?;
            verify_committed_journal(paths, platform, artifact_arch, state, &journal)?;
            remove_transaction_files(&journal_path, &work_dir)?;
            continue;
        }
        rollback(paths, platform, &journal, &journal_path, &work_dir)?;
    }
    Ok(())
}

fn verify_committed_journal(
    paths: &UserPaths,
    platform: Platform,
    artifact_arch: &str,
    state: &InstallState,
    journal: &Journal,
) -> Result<(), InstallTransactionError> {
    if state.platform != platform || state.artifact_arch != artifact_arch {
        return Err(InstallTransactionError::RecoveryConflict);
    }
    let expected_release = release_dir(
        &paths.install_root,
        &state.active_version,
        platform,
        &state.artifact_arch,
    );
    if journal.release_dir != expected_release
        || journal.binary_path != expected_release.join(binary_name(platform))
        || journal.artifact_sha256 != state.active_artifact_sha256
    {
        return Err(InstallTransactionError::RecoveryConflict);
    }
    verify_binary(
        &journal.binary_path,
        &state.active_artifact_sha256,
        state.active_artifact_size,
    )?;
    let state_registrations = state
        .registrations
        .iter()
        .map(|record| (record.path_sha256.as_str(), record.manifest_sha256.as_str()))
        .collect::<BTreeMap<_, _>>();
    for record in &journal.registrations {
        let path_digest = path_sha256(&record.target);
        match record.new_sha256.as_deref() {
            Some(new_digest)
                if state_registrations.get(path_digest.as_str()).copied() == Some(new_digest)
                    && hash_regular_file(&record.target, MAX_STATE_BYTES as u64)?.as_deref()
                        == Some(new_digest) => {}
            None if !state_registrations.contains_key(path_digest.as_str())
                && hash_regular_file(&record.target, MAX_STATE_BYTES as u64)?.is_none() => {}
            _ => return Err(InstallTransactionError::RecoveryConflict),
        }
    }
    Ok(())
}

fn rollback(
    paths: &UserPaths,
    platform: Platform,
    journal: &Journal,
    journal_path: &Path,
    work_dir: &Path,
) -> Result<(), InstallTransactionError> {
    let allowed = all_registration_paths(platform, paths)?;
    for record in journal.registrations.iter().rev() {
        if !allowed.contains(&record.target) {
            return Err(InstallTransactionError::JournalInvalid);
        }
        if record.backup.as_ref().is_some_and(|backup| {
            backup.parent() != Some(work_dir)
                || !backup
                    .file_name()
                    .and_then(|value| value.to_str())
                    .is_some_and(|name| {
                        name.starts_with("registration-") && name.ends_with(".backup")
                    })
        }) {
            return Err(InstallTransactionError::JournalInvalid);
        }
        let current = hash_regular_file(&record.target, MAX_STATE_BYTES as u64)?;
        match (&record.prior_sha256, &record.new_sha256, current.as_deref()) {
            (Some(prior), _, Some(current)) if current == prior => {}
            (None, _, None) => {}
            (Some(prior), Some(new), Some(current)) if current == new => {
                restore_backup(record, prior)?;
            }
            (Some(prior), None, None) => restore_backup(record, prior)?,
            (None, Some(new), Some(current)) if current == new => {
                fs::remove_file(&record.target).map_err(|_| InstallTransactionError::Filesystem)?;
                sync_dir(
                    record
                        .target
                        .parent()
                        .ok_or(InstallTransactionError::Filesystem)?,
                )?;
            }
            _ => return Err(InstallTransactionError::RecoveryConflict),
        }
        if record.parent_created {
            let parent = record
                .target
                .parent()
                .ok_or(InstallTransactionError::JournalInvalid)?;
            let mut entries =
                fs::read_dir(parent).map_err(|_| InstallTransactionError::Filesystem)?;
            if entries.next().is_none() {
                fs::remove_dir(parent).map_err(|_| InstallTransactionError::Filesystem)?;
                if let Some(grandparent) = parent.parent() {
                    sync_dir(grandparent)?;
                }
            }
        }
    }
    if journal.release_was_created && journal.release_dir.exists() {
        let expected_release = release_dir_from_journal(paths, platform, journal)?;
        let owner_marker = journal.release_dir.join("install-owner-v1");
        let expected_marker = json::object(&[
            ("transaction_id", json::quote(&journal.transaction_id)),
            ("artifact_sha256", json::quote(&journal.artifact_sha256)),
        ]);
        if expected_release != journal.release_dir
            || hash_regular_file(&journal.binary_path, MAX_PAYLOAD_BYTES)?.as_deref()
                != Some(journal.artifact_sha256.as_str())
            || read_bounded(&owner_marker, MAX_STATE_BYTES)? != expected_marker.as_bytes()
        {
            return Err(InstallTransactionError::RecoveryConflict);
        }
        fs::remove_dir_all(&journal.release_dir)
            .map_err(|_| InstallTransactionError::Filesystem)?;
        sync_dir(
            journal
                .release_dir
                .parent()
                .ok_or(InstallTransactionError::Filesystem)?,
        )?;
    }
    remove_transaction_files(journal_path, work_dir)
}

fn restore_backup(
    record: &JournalRegistration,
    prior_sha256: &str,
) -> Result<(), InstallTransactionError> {
    let backup = record
        .backup
        .as_ref()
        .ok_or(InstallTransactionError::JournalInvalid)?;
    if hash_regular_file(backup, MAX_STATE_BYTES as u64)?.as_deref() != Some(prior_sha256) {
        return Err(InstallTransactionError::RecoveryConflict);
    }
    let bytes = read_bounded(backup, MAX_STATE_BYTES)?;
    atomic_write(&record.target, &bytes, 0o600)
}

fn release_dir_from_journal(
    paths: &UserPaths,
    platform: Platform,
    journal: &Journal,
) -> Result<PathBuf, InstallTransactionError> {
    let releases = paths.install_root.join("releases");
    if !journal.release_dir.starts_with(&releases)
        || journal.release_dir.parent().and_then(Path::parent) != Some(releases.as_path())
        || journal.binary_path.parent() != Some(journal.release_dir.as_path())
        || journal
            .binary_path
            .file_name()
            .and_then(|value| value.to_str())
            != Some(binary_name(platform))
    {
        return Err(InstallTransactionError::JournalInvalid);
    }
    Ok(journal.release_dir.clone())
}

fn remove_transaction_files(
    journal_path: &Path,
    work_dir: &Path,
) -> Result<(), InstallTransactionError> {
    if work_dir.exists() {
        fs::remove_dir_all(work_dir).map_err(|_| InstallTransactionError::Filesystem)?;
    }
    match fs::remove_file(journal_path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(_) => return Err(InstallTransactionError::Filesystem),
    }
    if let Some(parent) = journal_path.parent() {
        sync_dir(parent)?;
    }
    Ok(())
}

fn prune_superseded_release(
    install_root: &Path,
    platform: Platform,
    artifact_arch: &str,
    old_state: Option<&InstallState>,
    new_state: &InstallState,
) -> Result<(), InstallTransactionError> {
    let Some(old_previous) = old_state.and_then(|state| state.previous.as_ref()) else {
        return Ok(());
    };
    if new_state
        .previous
        .as_ref()
        .map(|release| release.version.as_str())
        == Some(old_previous.version.as_str())
        || old_previous.version == new_state.active_version
    {
        return Ok(());
    }
    let directory = release_dir(install_root, &old_previous.version, platform, artifact_arch);
    let binary = directory.join(binary_name(platform));
    match hash_regular_file(&binary, MAX_PAYLOAD_BYTES)? {
        Some(digest) if digest == old_previous.sha256 => {
            fs::remove_dir_all(&directory).map_err(|_| InstallTransactionError::Filesystem)?;
            if let Some(parent) = directory.parent() {
                sync_dir(parent)?;
            }
        }
        None if !directory.exists() => {}
        _ => return Err(InstallTransactionError::RecoveryConflict),
    }
    Ok(())
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PreviousRelease {
    version: String,
    sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OwnedRegistration {
    path_sha256: String,
    manifest_sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct InstallState {
    install_id: String,
    active_version: String,
    active_artifact_sha256: String,
    active_artifact_size: u64,
    platform: Platform,
    artifact_arch: String,
    previous: Option<PreviousRelease>,
    installed_at_ms: u64,
    source: InstallSource,
    registered_browsers: Vec<BrowserId>,
    release_catalog_key_id: String,
    registrations: Vec<OwnedRegistration>,
    last_transaction_id: String,
}

impl InstallState {
    fn matches_candidate(
        &self,
        candidate: &FullyVerifiedInstallCandidate,
        browsers: &[BrowserId],
        registrations: &BTreeMap<PathBuf, BTreeSet<BrowserId>>,
        manifest_digest: &str,
    ) -> bool {
        self.active_version == candidate.version
            && self.active_artifact_sha256 == candidate.artifact_sha256
            && self.active_artifact_size == candidate.artifact_size
            && self.platform == candidate.platform
            && self.artifact_arch == candidate.artifact_arch
            && self.release_catalog_key_id == candidate.release_catalog_key_id
            && self.registered_browsers == browsers
            && self.registrations.len() == registrations.len()
            && self
                .registrations
                .iter()
                .all(|record| record.manifest_sha256 == manifest_digest)
    }

    fn encode(&self) -> String {
        let previous = self.previous.as_ref().map_or_else(
            || "null".to_owned(),
            |previous| {
                json::object(&[
                    ("version", json::quote(&previous.version)),
                    ("sha256", json::quote(&previous.sha256)),
                ])
            },
        );
        let browsers = json::string_array(
            self.registered_browsers
                .iter()
                .map(|browser| browser.as_str()),
        );
        let registrations = self
            .registrations
            .iter()
            .map(|record| {
                json::object(&[
                    ("path_sha256", json::quote(&record.path_sha256)),
                    ("manifest_sha256", json::quote(&record.manifest_sha256)),
                ])
            })
            .collect::<Vec<_>>()
            .join(",");
        json::object(&[
            ("schema_version", "1".to_owned()),
            ("install_id", json::quote(&self.install_id)),
            ("channel", json::quote("stable")),
            ("active_version", json::quote(&self.active_version)),
            (
                "active_artifact_sha256",
                json::quote(&self.active_artifact_sha256),
            ),
            (
                "active_artifact_size",
                self.active_artifact_size.to_string(),
            ),
            ("platform", json::quote(self.platform.as_str())),
            ("artifact_arch", json::quote(&self.artifact_arch)),
            ("previous", previous),
            ("installed_at_ms", self.installed_at_ms.to_string()),
            ("source", json::quote(self.source.as_str())),
            ("registered_browsers", browsers),
            (
                "release_catalog_key_id",
                json::quote(&self.release_catalog_key_id),
            ),
            ("registrations", format!("[{registrations}]")),
            (
                "last_transaction_id",
                json::quote(&self.last_transaction_id),
            ),
        ])
    }
}

fn load_state(path: &Path) -> Result<Option<InstallState>, InstallTransactionError> {
    if !path.exists() {
        return Ok(None);
    }
    let bytes = read_bounded(path, MAX_STATE_BYTES)?;
    let value = json::parse(&bytes).map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    parse_state(&value).map(Some)
}

fn parse_state(value: &Value) -> Result<InstallState, InstallTransactionError> {
    let object = exact_object(
        value,
        &[
            "schema_version",
            "install_id",
            "channel",
            "active_version",
            "active_artifact_sha256",
            "active_artifact_size",
            "platform",
            "artifact_arch",
            "previous",
            "installed_at_ms",
            "source",
            "registered_browsers",
            "release_catalog_key_id",
            "registrations",
            "last_transaction_id",
        ],
        InstallTransactionError::OwnedStateInvalid,
    )?;
    if number(object, "schema_version")? != 1 || text(object, "channel")? != "stable" {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let install_id = bounded_id(text(object, "install_id")?)?;
    let active_version = text(object, "active_version")?.to_owned();
    let active_artifact_sha256 = text(object, "active_artifact_sha256")?.to_owned();
    let active_artifact_size = number(object, "active_artifact_size")?;
    let platform = match text(object, "platform")? {
        "macos" => Platform::Macos,
        "linux" => Platform::Linux,
        _ => return Err(InstallTransactionError::OwnedStateInvalid),
    };
    let artifact_arch = text(object, "artifact_arch")?.to_owned();
    if !valid_version(&active_version)
        || !valid_sha256(&active_artifact_sha256)
        || active_artifact_size == 0
        || active_artifact_size > MAX_PAYLOAD_BYTES
        || !matches!(
            (platform, artifact_arch.as_str()),
            (Platform::Macos, "universal2")
                | (Platform::Linux, "x86_64")
                | (Platform::Linux, "aarch64")
        )
    {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let previous = match object.get("previous") {
        Some(Value::Null) => None,
        Some(value) => {
            let previous = exact_object(
                value,
                &["version", "sha256"],
                InstallTransactionError::OwnedStateInvalid,
            )?;
            let version = text(previous, "version")?.to_owned();
            let sha256 = text(previous, "sha256")?.to_owned();
            if !valid_version(&version) || !valid_sha256(&sha256) {
                return Err(InstallTransactionError::OwnedStateInvalid);
            }
            Some(PreviousRelease { version, sha256 })
        }
        None => return Err(InstallTransactionError::OwnedStateInvalid),
    };
    let source = match text(object, "source")? {
        "a0_cli" => InstallSource::A0Cli,
        "interactive_installer" => InstallSource::InteractiveInstaller,
        _ => return Err(InstallTransactionError::OwnedStateInvalid),
    };
    let registered_browsers = object
        .get("registered_browsers")
        .and_then(Value::as_array)
        .ok_or(InstallTransactionError::OwnedStateInvalid)?
        .iter()
        .map(|value| {
            value
                .as_str()
                .and_then(BrowserId::parse)
                .ok_or(InstallTransactionError::OwnedStateInvalid)
        })
        .collect::<Result<Vec<_>, _>>()?;
    if registered_browsers.is_empty()
        || registered_browsers.len() > BROWSERS.len()
        || registered_browsers
            .iter()
            .copied()
            .collect::<BTreeSet<_>>()
            .len()
            != registered_browsers.len()
    {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let registrations = object
        .get("registrations")
        .and_then(Value::as_array)
        .ok_or(InstallTransactionError::OwnedStateInvalid)?
        .iter()
        .map(|value| {
            let registration = exact_object(
                value,
                &["path_sha256", "manifest_sha256"],
                InstallTransactionError::OwnedStateInvalid,
            )?;
            let path_sha256 = text(registration, "path_sha256")?.to_owned();
            let manifest_sha256 = text(registration, "manifest_sha256")?.to_owned();
            if !valid_sha256(&path_sha256) || !valid_sha256(&manifest_sha256) {
                return Err(InstallTransactionError::OwnedStateInvalid);
            }
            Ok(OwnedRegistration {
                path_sha256,
                manifest_sha256,
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    if registrations.is_empty() || registrations.len() > BROWSERS.len() + 1 {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let release_catalog_key_id = text(object, "release_catalog_key_id")?.to_owned();
    if !valid_identifier(&release_catalog_key_id, 128) {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    Ok(InstallState {
        install_id,
        active_version,
        active_artifact_sha256,
        active_artifact_size,
        platform,
        artifact_arch,
        previous,
        installed_at_ms: number(object, "installed_at_ms")?,
        source,
        registered_browsers,
        release_catalog_key_id,
        registrations,
        last_transaction_id: bounded_id(text(object, "last_transaction_id")?)?,
    })
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct JournalRegistration {
    target: PathBuf,
    prior_sha256: Option<String>,
    new_sha256: Option<String>,
    backup: Option<PathBuf>,
    parent_created: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Journal {
    transaction_id: String,
    release_dir: PathBuf,
    binary_path: PathBuf,
    artifact_sha256: String,
    release_was_created: bool,
    registrations: Vec<JournalRegistration>,
}

impl Journal {
    fn encode(&self) -> Result<String, InstallTransactionError> {
        let registrations = self
            .registrations
            .iter()
            .map(|record| {
                Ok(json::object(&[
                    ("target", json::quote(path_text(&record.target)?)),
                    (
                        "prior_sha256",
                        optional_text(record.prior_sha256.as_deref()),
                    ),
                    ("new_sha256", optional_text(record.new_sha256.as_deref())),
                    (
                        "backup",
                        optional_text(record.backup.as_deref().map(path_text).transpose()?),
                    ),
                    ("parent_created", record.parent_created.to_string()),
                ]))
            })
            .collect::<Result<Vec<_>, InstallTransactionError>>()?
            .join(",");
        Ok(json::object(&[
            ("schema_version", "1".to_owned()),
            ("transaction_id", json::quote(&self.transaction_id)),
            ("release_dir", json::quote(path_text(&self.release_dir)?)),
            ("binary_path", json::quote(path_text(&self.binary_path)?)),
            ("artifact_sha256", json::quote(&self.artifact_sha256)),
            ("release_was_created", self.release_was_created.to_string()),
            ("registrations", format!("[{registrations}]")),
        ]))
    }
}

fn write_journal(path: &Path, journal: &Journal) -> Result<(), InstallTransactionError> {
    atomic_write(path, journal.encode()?.as_bytes(), 0o600)
}

fn load_journal(path: &Path) -> Result<Journal, InstallTransactionError> {
    let bytes = read_bounded(path, MAX_STATE_BYTES)?;
    let value = json::parse(&bytes).map_err(|_| InstallTransactionError::JournalInvalid)?;
    let object = exact_object(
        &value,
        &[
            "schema_version",
            "transaction_id",
            "release_dir",
            "binary_path",
            "artifact_sha256",
            "release_was_created",
            "registrations",
        ],
        InstallTransactionError::JournalInvalid,
    )?;
    if number(object, "schema_version").map_err(|_| InstallTransactionError::JournalInvalid)? != 1 {
        return Err(InstallTransactionError::JournalInvalid);
    }
    let transaction_id = bounded_id(
        text(object, "transaction_id").map_err(|_| InstallTransactionError::JournalInvalid)?,
    )
    .map_err(|_| InstallTransactionError::JournalInvalid)?;
    let release_dir = absolute_path(
        text(object, "release_dir").map_err(|_| InstallTransactionError::JournalInvalid)?,
    )?;
    let binary_path = absolute_path(
        text(object, "binary_path").map_err(|_| InstallTransactionError::JournalInvalid)?,
    )?;
    let artifact_sha256 = text(object, "artifact_sha256")
        .map_err(|_| InstallTransactionError::JournalInvalid)?
        .to_owned();
    if !valid_sha256(&artifact_sha256) {
        return Err(InstallTransactionError::JournalInvalid);
    }
    let release_was_created = match object.get("release_was_created") {
        Some(Value::Bool(value)) => *value,
        _ => return Err(InstallTransactionError::JournalInvalid),
    };
    let registrations = object
        .get("registrations")
        .and_then(Value::as_array)
        .ok_or(InstallTransactionError::JournalInvalid)?;
    if registrations.len() > BROWSERS.len() * 2 {
        return Err(InstallTransactionError::JournalInvalid);
    }
    let registrations = registrations
        .iter()
        .map(parse_journal_registration)
        .collect::<Result<Vec<_>, _>>()?;
    if registrations
        .iter()
        .map(|record| &record.target)
        .collect::<BTreeSet<_>>()
        .len()
        != registrations.len()
        || registrations
            .iter()
            .filter_map(|record| record.backup.as_ref())
            .collect::<BTreeSet<_>>()
            .len()
            != registrations
                .iter()
                .filter(|record| record.backup.is_some())
                .count()
    {
        return Err(InstallTransactionError::JournalInvalid);
    }
    Ok(Journal {
        transaction_id,
        release_dir,
        binary_path,
        artifact_sha256,
        release_was_created,
        registrations,
    })
}

fn parse_journal_registration(
    value: &Value,
) -> Result<JournalRegistration, InstallTransactionError> {
    let object = exact_object(
        value,
        &[
            "target",
            "prior_sha256",
            "new_sha256",
            "backup",
            "parent_created",
        ],
        InstallTransactionError::JournalInvalid,
    )?;
    let prior_sha256 = optional_sha(object.get("prior_sha256"))?;
    let new_sha256 = optional_sha(object.get("new_sha256"))?;
    if prior_sha256.is_none() && new_sha256.is_none() {
        return Err(InstallTransactionError::JournalInvalid);
    }
    let backup = match object.get("backup") {
        Some(Value::Null) => None,
        Some(Value::String(path)) => Some(absolute_path(path)?),
        _ => return Err(InstallTransactionError::JournalInvalid),
    };
    if prior_sha256.is_some() != backup.is_some() {
        return Err(InstallTransactionError::JournalInvalid);
    }
    let parent_created = match object.get("parent_created") {
        Some(Value::Bool(value)) => *value,
        _ => return Err(InstallTransactionError::JournalInvalid),
    };
    if parent_created && (prior_sha256.is_some() || new_sha256.is_none()) {
        return Err(InstallTransactionError::JournalInvalid);
    }
    Ok(JournalRegistration {
        target: absolute_path(
            text(object, "target").map_err(|_| InstallTransactionError::JournalInvalid)?,
        )?,
        prior_sha256,
        new_sha256,
        backup,
        parent_created,
    })
}

fn optional_sha(value: Option<&Value>) -> Result<Option<String>, InstallTransactionError> {
    match value {
        Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if valid_sha256(value) => Ok(Some(value.clone())),
        _ => Err(InstallTransactionError::JournalInvalid),
    }
}

struct InstallLock {
    file: File,
}

impl InstallLock {
    fn acquire(path: &Path) -> Result<Self, InstallTransactionError> {
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .open(path)
            .map_err(|_| InstallTransactionError::PrivateInstallRootUnavailable)?;
        set_mode(path, 0o600)?;
        for attempt in 0..LOCK_ATTEMPTS {
            if try_flock_exclusive(&file)? {
                file.set_len(0)
                    .map_err(|_| InstallTransactionError::Filesystem)?;
                file.write_all(b"a0-browser-bridge-install-lock-v1\n")
                    .map_err(|_| InstallTransactionError::Filesystem)?;
                file.sync_all()
                    .map_err(|_| InstallTransactionError::Filesystem)?;
                return Ok(Self { file });
            }
            if attempt + 1 < LOCK_ATTEMPTS {
                thread::sleep(LOCK_WAIT);
            }
        }
        Err(InstallTransactionError::InstallBusy)
    }
}

impl Drop for InstallLock {
    fn drop(&mut self) {
        let _ = flock_unlock(&self.file);
    }
}

#[cfg(unix)]
fn try_flock_exclusive(file: &File) -> Result<bool, InstallTransactionError> {
    use std::os::fd::AsRawFd;
    const LOCK_EX: i32 = 2;
    const LOCK_NB: i32 = 4;
    unsafe extern "C" {
        fn flock(fd: i32, operation: i32) -> i32;
    }
    let result = unsafe { flock(file.as_raw_fd(), LOCK_EX | LOCK_NB) };
    if result == 0 {
        Ok(true)
    } else if io::Error::last_os_error().kind() == io::ErrorKind::WouldBlock {
        Ok(false)
    } else {
        Err(InstallTransactionError::Filesystem)
    }
}

#[cfg(unix)]
fn flock_unlock(file: &File) -> Result<(), InstallTransactionError> {
    use std::os::fd::AsRawFd;
    const LOCK_UN: i32 = 8;
    unsafe extern "C" {
        fn flock(fd: i32, operation: i32) -> i32;
    }
    if unsafe { flock(file.as_raw_fd(), LOCK_UN) } == 0 {
        Ok(())
    } else {
        Err(InstallTransactionError::Filesystem)
    }
}

#[cfg(not(unix))]
fn try_flock_exclusive(_file: &File) -> Result<bool, InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

#[cfg(not(unix))]
fn flock_unlock(_file: &File) -> Result<(), InstallTransactionError> {
    Ok(())
}

fn validate_user_paths(
    paths: &UserPaths,
    platform: Platform,
) -> Result<(), InstallTransactionError> {
    if !paths.install_root.is_absolute() {
        return Err(InstallTransactionError::PrivateInstallRootUnavailable);
    }
    match platform {
        Platform::Macos
            if paths
                .home_root
                .as_ref()
                .is_some_and(|path| path.is_absolute()) =>
        {
            Ok(())
        }
        Platform::Linux
            if paths
                .config_root
                .as_ref()
                .is_some_and(|path| path.is_absolute()) =>
        {
            Ok(())
        }
        _ => Err(InstallTransactionError::UserPathsUnavailable),
    }
}

fn ensure_private_root(path: &Path) -> Result<(), InstallTransactionError> {
    if !path.is_absolute() {
        return Err(InstallTransactionError::PrivateInstallRootUnavailable);
    }
    if path.exists() {
        reject_symlink_or_non_directory(path)?;
    } else {
        fs::create_dir_all(path)
            .map_err(|_| InstallTransactionError::PrivateInstallRootUnavailable)?;
    }
    set_mode(path, 0o700)?;
    reject_symlink_or_non_directory(path)
}

fn ensure_private_dir(path: &Path) -> Result<(), InstallTransactionError> {
    if path.exists() {
        reject_symlink_or_non_directory(path)?;
    } else {
        fs::create_dir(path).map_err(|_| InstallTransactionError::Filesystem)?;
    }
    set_mode(path, 0o700)?;
    reject_symlink_or_non_directory(path)
}

fn ensure_new_private_dir(path: &Path) -> Result<(), InstallTransactionError> {
    fs::create_dir(path).map_err(|error| {
        if error.kind() == io::ErrorKind::AlreadyExists {
            InstallTransactionError::RecoveryConflict
        } else {
            InstallTransactionError::Filesystem
        }
    })?;
    set_mode(path, 0o700)
}

fn reject_symlink_or_non_directory(path: &Path) -> Result<(), InstallTransactionError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| InstallTransactionError::PrivateInstallRootUnavailable)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        Err(InstallTransactionError::PrivateInstallRootUnavailable)
    } else {
        Ok(())
    }
}

fn create_new_file(path: &Path, mode: u32) -> Result<File, InstallTransactionError> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| InstallTransactionError::Filesystem)?;
    set_mode(path, mode)?;
    Ok(file)
}

fn registration_parent_will_be_created(
    target: &Path,
    writing: bool,
) -> Result<bool, InstallTransactionError> {
    let parent = target.parent().ok_or(InstallTransactionError::Filesystem)?;
    match fs::symlink_metadata(parent) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            Err(InstallTransactionError::RegistrationConflict)
        }
        Ok(_) => Ok(false),
        Err(error) if error.kind() == io::ErrorKind::NotFound && !writing => Ok(false),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let grandparent = parent.parent().ok_or(InstallTransactionError::Filesystem)?;
            let metadata = fs::symlink_metadata(grandparent)
                .map_err(|_| InstallTransactionError::RegistrationUnavailable)?;
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                Err(InstallTransactionError::RegistrationConflict)
            } else {
                Ok(true)
            }
        }
        Err(_) => Err(InstallTransactionError::Filesystem),
    }
}

fn create_registration_parent(target: &Path) -> Result<(), InstallTransactionError> {
    let parent = target.parent().ok_or(InstallTransactionError::Filesystem)?;
    fs::create_dir(parent).map_err(|error| {
        if error.kind() == io::ErrorKind::AlreadyExists {
            InstallTransactionError::RegistrationConflict
        } else {
            InstallTransactionError::Filesystem
        }
    })?;
    set_mode(parent, 0o700)?;
    let metadata = fs::symlink_metadata(parent).map_err(|_| InstallTransactionError::Filesystem)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(InstallTransactionError::RegistrationConflict);
    }
    sync_dir(parent.parent().ok_or(InstallTransactionError::Filesystem)?)
}

fn atomic_write(path: &Path, bytes: &[u8], mode: u32) -> Result<(), InstallTransactionError> {
    let parent = path.parent().ok_or(InstallTransactionError::Filesystem)?;
    if !parent.exists() {
        return Err(InstallTransactionError::Filesystem);
    }
    let metadata = fs::symlink_metadata(parent).map_err(|_| InstallTransactionError::Filesystem)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(InstallTransactionError::Filesystem);
    }
    if path.exists() {
        let metadata =
            fs::symlink_metadata(path).map_err(|_| InstallTransactionError::Filesystem)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(InstallTransactionError::Filesystem);
        }
    }
    for _ in 0..16 {
        let temp = parent.join(format!(".a0-install-{}.tmp", random_id()?));
        let mut file = match create_new_file(&temp, mode) {
            Ok(file) => file,
            Err(_) => continue,
        };
        let result = (|| {
            file.write_all(bytes)
                .map_err(|_| InstallTransactionError::Filesystem)?;
            file.sync_all()
                .map_err(|_| InstallTransactionError::Filesystem)?;
            fs::rename(&temp, path).map_err(|_| InstallTransactionError::Filesystem)?;
            sync_dir(parent)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        return result;
    }
    Err(InstallTransactionError::Filesystem)
}

fn copy_regular_file(
    source: &Path,
    destination: &Path,
    max_bytes: u64,
    mode: u32,
) -> Result<(), InstallTransactionError> {
    let bytes = read_bounded(source, max_bytes as usize)?;
    let mut output = create_new_file(destination, mode)?;
    output
        .write_all(&bytes)
        .map_err(|_| InstallTransactionError::Filesystem)?;
    output
        .sync_all()
        .map_err(|_| InstallTransactionError::Filesystem)
}

fn read_bounded(path: &Path, max_bytes: usize) -> Result<Vec<u8>, InstallTransactionError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| InstallTransactionError::Filesystem)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() > max_bytes as u64
    {
        return Err(InstallTransactionError::Filesystem);
    }
    let file = File::open(path).map_err(|_| InstallTransactionError::Filesystem)?;
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(max_bytes as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| InstallTransactionError::Filesystem)?;
    if bytes.len() > max_bytes {
        return Err(InstallTransactionError::Filesystem);
    }
    Ok(bytes)
}

fn hash_regular_file(
    path: &Path,
    max_bytes: u64,
) -> Result<Option<String>, InstallTransactionError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(InstallTransactionError::Filesystem),
    };
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() > max_bytes {
        return Err(InstallTransactionError::Filesystem);
    }
    let mut file = File::open(path).map_err(|_| InstallTransactionError::Filesystem)?;
    let (digest, size) = hash_reader(&mut file, max_bytes)?;
    if size != metadata.len() {
        return Err(InstallTransactionError::Filesystem);
    }
    Ok(Some(digest))
}

fn hash_reader<R: Read>(
    reader: &mut R,
    max_bytes: u64,
) -> Result<(String, u64), InstallTransactionError> {
    let mut hasher = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; 32 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|_| InstallTransactionError::Filesystem)?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or(InstallTransactionError::PayloadInvalid)?;
        if total > max_bytes {
            return Err(InstallTransactionError::PayloadInvalid);
        }
        hasher.update(&buffer[..read]);
    }
    Ok((hex(&hasher.finalize()), total))
}

fn verify_binary(path: &Path, digest: &str, size: u64) -> Result<(), InstallTransactionError> {
    let metadata =
        fs::symlink_metadata(path).map_err(|_| InstallTransactionError::PayloadInvalid)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() != size {
        return Err(InstallTransactionError::PayloadInvalid);
    }
    if !is_executable(&metadata) {
        return Err(InstallTransactionError::PayloadInvalid);
    }
    if hash_regular_file(path, MAX_PAYLOAD_BYTES)?.as_deref() != Some(digest) {
        return Err(InstallTransactionError::PayloadInvalid);
    }
    Ok(())
}

#[cfg(unix)]
fn payload_identity(file: &File) -> Result<PayloadIdentity, InstallTransactionError> {
    use std::os::unix::fs::MetadataExt;
    let metadata = file
        .metadata()
        .map_err(|_| InstallTransactionError::CandidatePayloadChanged)?;
    if !metadata.is_file() {
        return Err(InstallTransactionError::CandidatePayloadChanged);
    }
    Ok(PayloadIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        length: metadata.len(),
    })
}

#[cfg(not(unix))]
fn payload_identity(_file: &File) -> Result<PayloadIdentity, InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

fn verify_payload_identity(
    file: &File,
    expected: &PayloadIdentity,
) -> Result<(), InstallTransactionError> {
    if &payload_identity(file)? == expected {
        Ok(())
    } else {
        Err(InstallTransactionError::CandidatePayloadChanged)
    }
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> Result<(), InstallTransactionError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .map_err(|_| InstallTransactionError::Filesystem)
}

#[cfg(not(unix))]
fn set_mode(_path: &Path, _mode: u32) -> Result<(), InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

#[cfg(unix)]
fn is_executable(metadata: &fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn is_executable(_metadata: &fs::Metadata) -> bool {
    false
}

fn sync_dir(path: &Path) -> Result<(), InstallTransactionError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| InstallTransactionError::Filesystem)
}

fn expected_artifact_arch(platform: Platform, arch: &str) -> &'static str {
    match platform {
        Platform::Macos => "universal2",
        Platform::Linux if arch == "aarch64" => "aarch64",
        Platform::Linux if arch == "x86_64" => "x86_64",
        Platform::Windows if arch == "aarch64" => "arm64",
        Platform::Windows if arch == "x86_64" => "x86_64",
        _ => "unsupported",
    }
}

fn release_dir(
    install_root: &Path,
    version: &str,
    platform: Platform,
    artifact_arch: &str,
) -> PathBuf {
    install_root
        .join("releases")
        .join(version)
        .join(format!("{}-{artifact_arch}", platform.as_str()))
}

fn release_binary_path(
    install_root: &Path,
    version: &str,
    platform: Platform,
    artifact_arch: &str,
) -> PathBuf {
    release_dir(install_root, version, platform, artifact_arch).join(binary_name(platform))
}

fn binary_name(platform: Platform) -> &'static str {
    if platform == Platform::Windows {
        "a0-browser-bridge.exe"
    } else {
        "a0-browser-bridge"
    }
}

fn path_sha256(path: &Path) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        return sha256(path.as_os_str().as_bytes());
    }
    #[cfg(not(unix))]
    sha256(path.to_string_lossy().as_bytes())
}

fn path_text(path: &Path) -> Result<&str, InstallTransactionError> {
    path.to_str().ok_or(InstallTransactionError::Filesystem)
}

fn absolute_path(value: &str) -> Result<PathBuf, InstallTransactionError> {
    let path = PathBuf::from(value);
    if path.is_absolute() && value.len() <= 4096 {
        Ok(path)
    } else {
        Err(InstallTransactionError::JournalInvalid)
    }
}

fn optional_text(value: Option<&str>) -> String {
    value.map_or_else(|| "null".to_owned(), json::quote)
}

fn exact_object<'a>(
    value: &'a Value,
    keys: &[&str],
    error: InstallTransactionError,
) -> Result<&'a BTreeMap<String, Value>, InstallTransactionError> {
    let object = value.as_object().ok_or(error)?;
    if object.len() != keys.len() || keys.iter().any(|key| !object.contains_key(*key)) {
        return Err(error);
    }
    Ok(object)
}

fn text<'a>(
    object: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a str, InstallTransactionError> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 4096)
        .ok_or(InstallTransactionError::OwnedStateInvalid)
}

fn number(object: &BTreeMap<String, Value>, key: &str) -> Result<u64, InstallTransactionError> {
    object
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(InstallTransactionError::OwnedStateInvalid)
}

fn bounded_id(value: &str) -> Result<String, InstallTransactionError> {
    if value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(value.to_owned())
    } else {
        Err(InstallTransactionError::OwnedStateInvalid)
    }
}

fn valid_identifier(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_version(value: &str) -> bool {
    if value.is_empty() || value.len() > 64 || value.contains('/') || value.contains('\\') {
        return false;
    }
    let (base, build_valid) = value
        .split_once('+')
        .map_or((value, true), |(base, build)| {
            (
                base,
                !build.is_empty()
                    && build.split('.').all(|identifier| {
                        !identifier.is_empty()
                            && identifier
                                .bytes()
                                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                    }),
            )
        });
    let parts = base.split('.').collect::<Vec<_>>();
    build_valid
        && parts.len() == 3
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.len() <= 9
                && (part.len() == 1 || !part.starts_with('0'))
                && part.bytes().all(|byte| byte.is_ascii_digit())
        })
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'-'))
}

fn version_tuple(value: &str) -> (u32, u32, u32) {
    let base = value.split_once('+').map_or(value, |(base, _)| base);
    let mut parts = base
        .split('.')
        .map(|part| part.parse::<u32>().unwrap_or(u32::MAX));
    (
        parts.next().unwrap_or(u32::MAX),
        parts.next().unwrap_or(u32::MAX),
        parts.next().unwrap_or(u32::MAX),
    )
}

fn sha256(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

fn origin_set_sha256(origins: &[String]) -> String {
    let mut origins = origins.iter().map(String::as_str).collect::<Vec<_>>();
    origins.sort_unstable();
    let mut hasher = Sha256::new();
    for origin in origins {
        hasher.update((origin.len() as u64).to_be_bytes());
        hasher.update(origin.as_bytes());
    }
    hex(&hasher.finalize())
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn random_id() -> Result<String, InstallTransactionError> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| InstallTransactionError::EntropyUnavailable)?;
    Ok(hex(&bytes))
}

fn now_ms() -> Result<u64, InstallTransactionError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| InstallTransactionError::Filesystem)?
        .as_millis();
    u64::try_from(millis).map_err(|_| InstallTransactionError::Filesystem)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FixtureRoot(PathBuf);

    impl FixtureRoot {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("a0-install-test-{}", random_id().unwrap()));
            fs::create_dir(&path).unwrap();
            set_mode(&path, 0o700).unwrap();
            Self(path)
        }

        fn paths(&self, platform: Platform) -> UserPaths {
            let install_root = self.0.join("data/browser-bridge");
            match platform {
                Platform::Macos => UserPaths {
                    install_root,
                    home_root: Some(self.0.join("home")),
                    config_root: None,
                },
                Platform::Linux => UserPaths {
                    install_root,
                    home_root: Some(self.0.join("home")),
                    config_root: Some(self.0.join("config")),
                },
                _ => UserPaths {
                    install_root,
                    home_root: None,
                    config_root: None,
                },
            }
        }

        fn candidate(
            &self,
            version: &str,
            platform: Platform,
            bytes: &[u8],
        ) -> FullyVerifiedInstallCandidate {
            let payload = self.0.join(format!("payload-{version}"));
            fs::write(&payload, bytes).unwrap();
            let file = File::open(payload).unwrap();
            FullyVerifiedInstallCandidate::test_fixture(file, version, platform, "x86_64").unwrap()
        }
    }

    impl Drop for FixtureRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn install(
        fixture: &FixtureRoot,
        version: &str,
        browsers: Vec<BrowserId>,
        fault: Fault,
    ) -> Result<InstallTransactionResult, InstallTransactionError> {
        let platform = Platform::Linux;
        let paths = fixture.paths(platform);
        fs::create_dir_all(paths.config_root.as_ref().unwrap()).unwrap();
        prepare_registration_roots(&paths, platform, &browsers);
        execute_at(
            InstallOperation::Install,
            browsers.into_iter().map(BrowserTarget::Explicit).collect(),
            InstallSource::A0Cli,
            fixture.candidate(version, platform, format!("payload-{version}").as_bytes()),
            platform,
            "x86_64",
            paths,
            1_788_492_400_000,
            fault,
        )
    }

    fn prepare_registration_roots(paths: &UserPaths, platform: Platform, browsers: &[BrowserId]) {
        for browser in browsers {
            let RegistrationLocation::ManifestFiles(files) =
                registration_location(*browser, platform, &paths).unwrap()
            else {
                unreachable!()
            };
            for target in files {
                fs::create_dir_all(target.parent().unwrap().parent().unwrap()).unwrap();
            }
        }
    }

    #[test]
    fn read_only_installed_status_requires_signed_receipts_and_keeps_unpublished_trust_closed() {
        let fixture = FixtureRoot::new();
        install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        let paths = fixture.paths(Platform::Linux);
        let state_path = paths.install_root.join("install-state.json");
        let before = fs::read(&state_path).unwrap();
        let origins = ["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"];
        // A fixture install-state and correct payload bytes are not signed
        // release evidence. Unsigned state can never make status "installed".
        assert!(verify_installed_at(&paths, Platform::Linux, "x86_64", &origins).is_err());
        assert_eq!(fs::read(&state_path).unwrap(), before);
        assert!(matches!(
            verify_installed_status(&paths),
            Err(InstallTransactionError::ReleaseEvidenceUnavailable)
        ));

        let binary = release_binary_path(&paths.install_root, "2.12.0", Platform::Linux, "x86_64");
        set_mode(&binary, 0o700).unwrap();
        fs::write(&binary, b"PAYLOAD-2.12.0").unwrap();
        assert!(matches!(
            verify_installed_at(&paths, Platform::Linux, "x86_64", &origins),
            Err(InstallTransactionError::OwnedStateInvalid)
        ));
        fs::write(&binary, b"payload-2.12.0").unwrap();
        fs::write(paths.install_root.join("transactions/pending.json"), b"{}").unwrap();
        assert!(matches!(
            verify_installed_at(&paths, Platform::Linux, "x86_64", &origins),
            Err(InstallTransactionError::InstallRecoveryRequired)
        ));
    }

    #[test]
    fn verified_handle_installs_and_replays_idempotently() {
        let fixture = FixtureRoot::new();
        let first = install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        assert!(!first.already_current);
        assert_eq!(first.registration_count, 1);
        let state_before = fs::read(
            fixture
                .paths(Platform::Linux)
                .install_root
                .join("install-state.json"),
        )
        .unwrap();
        let second = install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        assert!(second.already_current);
        assert_eq!(
            fs::read(
                fixture
                    .paths(Platform::Linux)
                    .install_root
                    .join("install-state.json")
            )
            .unwrap(),
            state_before
        );
    }

    #[test]
    fn browser_discovery_auto_install_is_bounded_and_deduplicates_shared_targets() {
        let fixture = FixtureRoot::new();
        let platform = Platform::Linux;
        let paths = fixture.paths(platform);
        fs::create_dir_all(paths.config_root.as_ref().unwrap()).unwrap();
        prepare_registration_roots(&paths, platform, &[BrowserId::Chrome, BrowserId::Opera]);
        let result = execute_at_with_discovery(
            InstallOperation::Install,
            vec![BrowserTarget::Auto],
            InstallSource::A0Cli,
            fixture.candidate("2.12.0", platform, b"payload-auto"),
            platform,
            "x86_64",
            paths,
            1,
            Fault::None,
            |_platform, _paths| Ok(vec![BrowserId::Opera, BrowserId::Chrome, BrowserId::Chrome]),
        )
        .unwrap();
        assert_eq!(
            result.registered_browsers,
            vec![BrowserId::Chrome, BrowserId::Opera]
        );
        assert_eq!(result.registration_count, 2);

        let empty = FixtureRoot::new();
        let empty_paths = empty.paths(platform);
        assert_eq!(
            execute_at_with_discovery(
                InstallOperation::Install,
                vec![BrowserTarget::Auto],
                InstallSource::A0Cli,
                empty.candidate("2.12.0", platform, b"payload-empty"),
                platform,
                "x86_64",
                empty_paths.clone(),
                1,
                Fault::None,
                |_platform, _paths| Ok(Vec::new()),
            ),
            Err(InstallTransactionError::NoBrowserTargets)
        );
        assert_eq!(
            InstallTransactionError::NoBrowserTargets.exit_code(),
            EXIT_NOT_INSTALLED
        );
        assert!(!empty_paths.install_root.exists());
    }

    #[test]
    fn browser_discovery_auto_update_reuses_only_fully_validated_owned_targets() {
        let fixture = FixtureRoot::new();
        install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        let platform = Platform::Linux;
        let paths = fixture.paths(platform);
        let updated = execute_at_with_discovery(
            InstallOperation::Update,
            vec![BrowserTarget::Auto],
            InstallSource::A0Cli,
            fixture.candidate("2.13.0", platform, b"payload-update"),
            platform,
            "x86_64",
            paths.clone(),
            2,
            Fault::None,
            |_platform, _paths| Ok(Vec::new()),
        )
        .unwrap();
        assert_eq!(updated.registered_browsers, vec![BrowserId::Chrome]);

        let manifest = match registration_location(BrowserId::Chrome, platform, &paths).unwrap() {
            RegistrationLocation::ManifestFiles(files) => files[0].clone(),
            _ => unreachable!(),
        };
        fs::write(&manifest, b"changed owned registration").unwrap();
        assert_eq!(
            execute_at_with_discovery(
                InstallOperation::Update,
                vec![BrowserTarget::Auto],
                InstallSource::A0Cli,
                fixture.candidate("2.14.0", platform, b"payload-rejected-update"),
                platform,
                "x86_64",
                paths,
                3,
                Fault::None,
                |_platform, _paths| Ok(Vec::new()),
            ),
            Err(InstallTransactionError::OwnedRegistrationChanged)
        );
        assert_eq!(fs::read(manifest).unwrap(), b"changed owned registration");
    }

    #[test]
    fn foreign_manifest_is_never_overwritten() {
        let fixture = FixtureRoot::new();
        let paths = fixture.paths(Platform::Linux);
        let target =
            match registration_location(BrowserId::Chrome, Platform::Linux, &paths).unwrap() {
                RegistrationLocation::ManifestFiles(files) => files[0].clone(),
                _ => unreachable!(),
            };
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(&target, b"foreign").unwrap();
        assert_eq!(
            install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap_err(),
            InstallTransactionError::RegistrationConflict
        );
        assert_eq!(fs::read(target).unwrap(), b"foreign");
        assert!(!paths.install_root.join("install-state.json").exists());
    }

    #[test]
    fn retained_payload_handle_cannot_hide_in_place_byte_substitution() {
        let fixture = FixtureRoot::new();
        let platform = Platform::Linux;
        let paths = fixture.paths(platform);
        fs::create_dir_all(paths.config_root.as_ref().unwrap()).unwrap();
        let payload_path = fixture.0.join("mutable-payload");
        fs::write(&payload_path, b"trusted-payload").unwrap();
        let file = File::open(&payload_path).unwrap();
        let candidate =
            FullyVerifiedInstallCandidate::test_fixture(file, "2.12.0", platform, "x86_64")
                .unwrap();
        fs::write(&payload_path, b"changed-payload").unwrap();
        assert_eq!(
            execute_at(
                InstallOperation::Install,
                vec![BrowserTarget::Explicit(BrowserId::Chrome)],
                InstallSource::A0Cli,
                candidate,
                platform,
                "x86_64",
                paths.clone(),
                1,
                Fault::None,
            )
            .unwrap_err(),
            InstallTransactionError::PayloadInvalid
        );
        assert!(!paths.install_root.join("install-state.json").exists());
    }

    #[test]
    fn failed_second_phase_restores_all_prior_state() {
        let fixture = FixtureRoot::new();
        install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        let paths = fixture.paths(Platform::Linux);
        let state_before = fs::read(paths.install_root.join("install-state.json")).unwrap();
        let manifest =
            match registration_location(BrowserId::Chrome, Platform::Linux, &paths).unwrap() {
                RegistrationLocation::ManifestFiles(files) => files[0].clone(),
                _ => unreachable!(),
            };
        let manifest_before = fs::read(&manifest).unwrap();
        assert_eq!(
            install(
                &fixture,
                "2.13.0",
                vec![BrowserId::Chrome, BrowserId::Edge],
                Fault::FailAfterFirstRegistration
            )
            .unwrap_err(),
            InstallTransactionError::InjectedFailure
        );
        assert_eq!(
            fs::read(paths.install_root.join("install-state.json")).unwrap(),
            state_before
        );
        assert_eq!(fs::read(manifest).unwrap(), manifest_before);
        assert!(!release_dir(&paths.install_root, "2.13.0", Platform::Linux, "x86_64").exists());
    }

    #[test]
    fn next_run_recovers_interrupted_journal_before_installing() {
        let fixture = FixtureRoot::new();
        install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        assert_eq!(
            install(
                &fixture,
                "2.13.0",
                vec![BrowserId::Chrome, BrowserId::Edge],
                Fault::CrashAfterFirstRegistration
            )
            .unwrap_err(),
            InstallTransactionError::InjectedCrash
        );
        let recovered = install(
            &fixture,
            "2.13.0",
            vec![BrowserId::Chrome, BrowserId::Edge],
            Fault::None,
        )
        .unwrap();
        assert_eq!(recovered.active_version, "2.13.0");
        assert_eq!(recovered.registration_count, 2);
        let transactions = fixture
            .paths(Platform::Linux)
            .install_root
            .join("transactions");
        assert!(fs::read_dir(transactions).unwrap().next().is_none());
    }

    #[test]
    fn committed_state_makes_post_rename_recovery_roll_forward() {
        let fixture = FixtureRoot::new();
        assert_eq!(
            install(
                &fixture,
                "2.12.0",
                vec![BrowserId::Chrome],
                Fault::CrashAfterStateCommit,
            )
            .unwrap_err(),
            InstallTransactionError::InjectedCrash
        );
        let recovered = install(&fixture, "2.12.0", vec![BrowserId::Chrome], Fault::None).unwrap();
        assert!(recovered.already_current);
        let transactions = fixture
            .paths(Platform::Linux)
            .install_root
            .join("transactions");
        assert!(fs::read_dir(transactions).unwrap().next().is_none());
    }

    #[test]
    fn windows_never_uses_unix_manifest_transaction() {
        let fixture = FixtureRoot::new();
        let candidate = fixture.candidate("2.12.0", Platform::Windows, b"windows-payload");
        assert_eq!(
            execute_at(
                InstallOperation::Install,
                vec![BrowserTarget::Explicit(BrowserId::Chrome)],
                InstallSource::A0Cli,
                candidate,
                Platform::Windows,
                "x86_64",
                fixture.paths(Platform::Windows),
                1,
                Fault::None,
            )
            .unwrap_err(),
            InstallTransactionError::WindowsAdapterUnavailable
        );
    }
}
