//! Exact-handle composition of independent stable-release verification gates.
//!
//! This module is crate-private deliberately. Implementations of the verifier
//! are trusted release code, not plugin callbacks or a public evidence API.
//! No CLI, page, environment option or local-development build can supply a
//! verifier or skip a gate. A concrete Darwin/local-provenance path is included;
//! actual compiled publisher/builder identities and release acquisition remain
//! required before the production installer can invoke it.

#![allow(dead_code)] // Integrated only when reviewed release verifiers land.

use std::fs::File;

#[path = "release_provenance.rs"]
mod provenance;

#[path = "release_macho.rs"]
mod macho;

#[path = "release_elf.rs"]
mod elf;

#[cfg(any(target_os = "linux", test))]
#[path = "release_linux.rs"]
mod linux;

#[cfg(target_os = "macos")]
#[path = "release_macos.rs"]
mod macos;

/// Acquire a compiled immutable release and its detached local derivation
/// receipt, then perform every concrete Darwin gate before returning authority.
/// No GitHub API, latest lookup, arbitrary URL or caller-supplied evidence.
#[cfg(target_os = "macos")]
pub(crate) fn acquire_macos_production_candidate(
    version: &str,
    known_floor: &str,
    private_parent: &std::path::Path,
) -> Result<FullyVerifiedInstallCandidate, CompositionError> {
    if cfg!(feature = "local-development") || !crate::release::release_trust_configured() {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let catalog = crate::release::catalog::fetch_catalog(version, known_floor)
        .map_err(|_| CompositionError::CatalogMismatch)?;
    let artifact = catalog
        .artifacts()
        .iter()
        .find(|artifact| {
            artifact.kind == "payload"
                && artifact.platform == "macos"
                && artifact.arch == "universal2"
        })
        .ok_or(CompositionError::PlatformMismatch)?;
    let sources: Vec<_> = crate::release::PINNED_BUILD_PROVENANCE
        .iter()
        .filter(|source| {
            source.release == version
                && source.platform == "macos"
                && source.artifact_arch == "universal2"
        })
        .collect();
    if sources.len() != 1 {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let source = sources[0];
    if source.statement_url == source.signature_url
        || [source.statement_url, source.signature_url]
            .iter()
            .any(|url| !url.starts_with("https://") || !crate::rpc::valid_server_base_origin(url))
    {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let statement = crate::release::catalog::download_metadata(source.statement_url, 16 * 1024)
        .map_err(|_| CompositionError::ProvenanceRejected)?;
    let signature = crate::release::catalog::download_metadata(source.signature_url, 64)
        .map_err(|_| CompositionError::ProvenanceRejected)?;
    let payload =
        crate::release_payload::download_verified_payload(&catalog, &artifact.name, private_parent)
            .map_err(|_| CompositionError::PayloadChanged)?;
    compose_macos_production_candidate(&catalog, payload, &statement, &signature)
}

/// Concrete local-release macOS path; no GitHub Actions client or self-report
/// can replace the mandatory catalog, Developer ID/notary and builder proofs.
#[cfg(target_os = "macos")]
pub(crate) fn compose_macos_production_candidate(
    catalog: &VerifiedCatalog,
    mut payload: VerifiedExecutablePayload,
    local_provenance: &[u8],
    provenance_signature: &[u8],
) -> Result<FullyVerifiedInstallCandidate, CompositionError> {
    if cfg!(feature = "local-development") || !crate::release::release_trust_configured() {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let archive_sha256 = payload.archive_sha256().to_owned();
    let executable_sha256 = payload.executable_sha256().to_owned();
    let artifact_arch = payload.arch().to_owned();
    let expected = ExpectedRelease {
        version: catalog.release(),
        catalog_key_id: catalog.key_id(),
        catalog_sha256: catalog.digest(),
        archive_sha256: &archive_sha256,
        executable_sha256: &executable_sha256,
        executable_size: payload.executable_size(),
        platform: Platform::Macos,
        artifact_arch: &artifact_arch,
    };
    let mut verifier = macos::MacosCandidateVerifier::new(
        &mut payload,
        &expected,
        local_provenance,
        provenance_signature,
    )?;
    let provenance = verifier.retained_provenance();
    compose_production_candidate(catalog, payload, &mut verifier, provenance)
}

/// Stable Linux acquisition uses only the compiled immutable catalog and
/// detached provenance locations for this exact version/architecture.
#[cfg(target_os = "linux")]
pub(crate) fn acquire_linux_production_candidate(
    version: &str,
    known_floor: &str,
    private_parent: &std::path::Path,
) -> Result<FullyVerifiedInstallCandidate, CompositionError> {
    if cfg!(feature = "local-development") || !crate::release::release_trust_configured() {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let arch = expected_artifact_arch(Platform::Linux, architecture());
    if arch == "unsupported" {
        return Err(CompositionError::PlatformMismatch);
    }
    let catalog = crate::release::catalog::fetch_catalog(version, known_floor)
        .map_err(|_| CompositionError::CatalogMismatch)?;
    let artifacts: Vec<_> = catalog
        .artifacts()
        .iter()
        .filter(|artifact| {
            artifact.kind == "payload" && artifact.platform == "linux" && artifact.arch == arch
        })
        .collect();
    let sources: Vec<_> = crate::release::PINNED_BUILD_PROVENANCE
        .iter()
        .filter(|source| {
            source.release == version && source.platform == "linux" && source.artifact_arch == arch
        })
        .collect();
    if artifacts.len() != 1 || sources.len() != 1 {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let source = sources[0];
    if source.statement_url == source.signature_url
        || [source.statement_url, source.signature_url]
            .iter()
            .any(|url| !url.starts_with("https://") || !crate::rpc::valid_server_base_origin(url))
    {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let statement = crate::release::catalog::download_metadata(source.statement_url, 16 * 1024)
        .map_err(|_| CompositionError::ProvenanceRejected)?;
    let signature = crate::release::catalog::download_metadata(source.signature_url, 64)
        .map_err(|_| CompositionError::ProvenanceRejected)?;
    let payload = crate::release_payload::download_verified_payload(
        &catalog,
        &artifacts[0].name,
        private_parent,
    )
    .map_err(|_| CompositionError::PayloadChanged)?;
    compose_linux_production_candidate(&catalog, payload, &statement, &signature)
}

#[cfg(target_os = "linux")]
pub(crate) fn compose_linux_production_candidate(
    catalog: &VerifiedCatalog,
    payload: VerifiedExecutablePayload,
    statement: &[u8],
    signature: &[u8],
) -> Result<FullyVerifiedInstallCandidate, CompositionError> {
    let archive = payload.archive_sha256().to_owned();
    let executable = payload.executable_sha256().to_owned();
    let arch = payload.arch().to_owned();
    let expected = ExpectedRelease {
        version: catalog.release(),
        catalog_key_id: catalog.key_id(),
        catalog_sha256: catalog.digest(),
        archive_sha256: &archive,
        executable_sha256: &executable,
        executable_size: payload.executable_size(),
        platform: Platform::Linux,
        artifact_arch: &arch,
    };
    let mut verifier =
        linux::LinuxCandidateVerifier::new(catalog, &expected, statement, signature)?;
    let provenance = verifier.retained_provenance();
    compose_production_candidate(catalog, payload, &mut verifier, provenance)
}

use super::{
    expected_artifact_arch, origin_set_sha256, payload_identity, BoundVerification,
    CandidateAuthority, FullyVerifiedInstallCandidate,
};
use crate::json::{self, Value};
use crate::platform::{architecture, Platform};
use crate::release::catalog::VerifiedCatalog;
use crate::release_payload::VerifiedExecutablePayload;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum CompositionError {
    ReleaseTrustUnavailable,
    CatalogMismatch,
    PlatformMismatch,
    PayloadChanged,
    PlatformSignatureRejected,
    ProvenanceRejected,
    EmbeddedIdentityRejected,
    SelfTestRejected,
    CandidateRejected,
}

/// Values required from embedded metadata, independently of a process claiming
/// that its own version is correct. Adapters must obtain them from the supplied
/// handle, not the currently executing installer or a downloaded sidecar.
pub(crate) struct EmbeddedIdentity {
    pub version: String,
    pub platform: Platform,
    pub artifact_arch: String,
    pub native_host: String,
    pub protocol_version: u64,
    pub self_test_contract: String,
    pub install_contract: String,
}

/// Immutable expected release binding supplied to every independent verifier.
pub(crate) struct ExpectedRelease<'a> {
    pub version: &'a str,
    pub catalog_key_id: &'a str,
    pub catalog_sha256: &'a str,
    pub archive_sha256: &'a str,
    pub executable_sha256: &'a str,
    pub executable_size: u64,
    pub platform: Platform,
    pub artifact_arch: &'a str,
}

/// Detached authenticated evidence retained beside the installed executable.
/// Created only from cryptographic proofs, never unsigned install-state data.
pub(super) struct InstalledReleaseEvidence {
    catalog: Vec<u8>,
    catalog_signature: Vec<u8>,
    provenance: Vec<u8>,
    provenance_signature: Vec<u8>,
}

impl InstalledReleaseEvidence {
    pub(super) fn files(&self) -> [(&'static str, &[u8]); 4] {
        [
            ("release-catalog.json", &self.catalog),
            ("release-catalog.sig", &self.catalog_signature),
            ("build-provenance.json", &self.provenance),
            ("build-provenance.sig", &self.provenance_signature),
        ]
    }
}

/// Offline verification uses signer roots rather than self-referential binary
/// pins. The executable digest/size must be measured from its retained handle.
pub(super) fn verify_installed_evidence(
    catalog_bytes: &[u8],
    catalog_signature: &[u8],
    statement: &[u8],
    signature: &[u8],
    version: &str,
    key_id: &str,
    platform: Platform,
    arch: &str,
    executable_sha256: &str,
    executable_size: u64,
) -> Result<(), CompositionError> {
    let catalog = crate::release::catalog::verify_catalog(
        catalog_bytes,
        catalog_signature,
        crate::release::catalog::MINIMUM_SECURE_COMPANION,
    )
    .map_err(|_| CompositionError::CatalogMismatch)?;
    if catalog.release() != version
        || catalog.key_id() != key_id
        || crate::release::PINNED_RELEASE_CATALOGS
            .iter()
            .filter(|source| source.release == version)
            .count()
            != 1
    {
        return Err(CompositionError::CatalogMismatch);
    }
    let artifacts: Vec<_> = catalog
        .artifacts()
        .iter()
        .filter(|artifact| {
            artifact.kind == "payload"
                && artifact.platform == platform.as_str()
                && artifact.arch == arch
        })
        .collect();
    if artifacts.len() != 1 {
        return Err(CompositionError::CatalogMismatch);
    }
    provenance::verify_local_build_provenance(
        statement,
        signature,
        &ExpectedRelease {
            version,
            catalog_key_id: key_id,
            catalog_sha256: catalog.digest(),
            archive_sha256: &artifacts[0].sha256,
            executable_sha256,
            executable_size,
            platform,
            artifact_arch: arch,
        },
    )?;
    Ok(())
}

/// Only trusted, reviewed in-crate adapters may implement this interface.
/// A successful signature check includes the pinned publisher identity (and
/// notarization when required); provenance must bind the signed archive and
/// executable to the approved build identity. Linux still requires its explicit
/// release-signature policy, never an automatic platform-signature success.
/// Self-test execution must be offline, bounded in time/output, and use the
/// supplied file object. Darwin alone may use the generated private pathname
/// under its retained-descriptor verification lease; no caller-selected path or
/// unverified pathname substitution is allowed.
pub(crate) trait ReleaseCandidateVerifier {
    fn platform_signature(
        &mut self,
        executable: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError>;
    fn provenance(
        &mut self,
        executable: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError>;
    fn embedded_identity(
        &mut self,
        executable: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<EmbeddedIdentity, CompositionError>;
    fn offline_self_test(
        &mut self,
        executable: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<Vec<u8>, CompositionError>;
}

pub(crate) fn compose_production_candidate(
    catalog: &VerifiedCatalog,
    mut payload: VerifiedExecutablePayload,
    verifier: &mut impl ReleaseCandidateVerifier,
    provenance: provenance::VerifiedBuildProvenance,
) -> Result<FullyVerifiedInstallCandidate, CompositionError> {
    // Compile-selected development builds can never mint stable install
    // authority, even if production roots become configured in a later build.
    if cfg!(feature = "local-development") || !crate::release::release_trust_configured() {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    if payload.catalog_proof().catalog_digest() != catalog.digest()
        || !catalog
            .artifacts()
            .contains(payload.catalog_proof().artifact())
        || !crate::release::TRUSTED_RELEASE_PUBLIC_KEYS
            .iter()
            .any(|key| key.key_id == catalog.key_id())
    {
        return Err(CompositionError::CatalogMismatch);
    }
    let platform = Platform::current();
    if platform == Platform::Unsupported
        || payload.platform() != platform.as_str()
        || payload.arch() != expected_artifact_arch(platform, architecture())
    {
        return Err(CompositionError::PlatformMismatch);
    }

    // Owned strings keep the expected binding immutable while the retained
    // proof is mutably rehashed between each verifier.
    let archive_sha256 = payload.archive_sha256().to_owned();
    let executable_sha256 = payload.executable_sha256().to_owned();
    let artifact_arch = payload.arch().to_owned();
    let expected = ExpectedRelease {
        version: catalog.release(),
        catalog_key_id: catalog.key_id(),
        catalog_sha256: catalog.digest(),
        archive_sha256: &archive_sha256,
        executable_sha256: &executable_sha256,
        executable_size: payload.executable_size(),
        platform,
        artifact_arch: &artifact_arch,
    };
    verify_independent_gates(&mut payload, &expected, verifier)?;
    if !provenance.matches(&expected) {
        return Err(CompositionError::ProvenanceRejected);
    }
    let (catalog_bytes, catalog_signature) = catalog.signed_evidence();
    let (statement, signature) = provenance.signed_evidence();
    let installed_evidence = InstalledReleaseEvidence {
        catalog: catalog_bytes.to_vec(),
        catalog_signature: catalog_signature.to_vec(),
        provenance: statement.to_vec(),
        provenance_signature: signature.to_vec(),
    };
    let executable = payload
        .verification_handle()
        .map_err(|_| CompositionError::PayloadChanged)?;
    let identity = payload_identity(&executable).map_err(|_| CompositionError::PayloadChanged)?;
    let allowed_origins: Vec<String> = crate::release::PRODUCTION_EXTENSION_ORIGINS
        .iter()
        .map(|origin| (*origin).to_owned())
        .collect();
    let bound = BoundVerification {
        release: catalog.release().to_owned(),
        release_catalog_key_id: catalog.key_id().to_owned(),
        catalog_sha256: catalog.digest().to_owned(),
        // The transaction's historical artifact fields describe the installed
        // executable, not the compressed catalog artifact. The retained proof
        // preserves the independent archive-to-executable derivation.
        artifact_sha256: executable_sha256.clone(),
        platform,
        artifact_arch: artifact_arch.clone(),
        origin_set_sha256: origin_set_sha256(&allowed_origins),
        payload: identity.clone(),
    };
    let candidate = FullyVerifiedInstallCandidate {
        version: catalog.release().to_owned(),
        release_catalog_key_id: catalog.key_id().to_owned(),
        catalog_sha256: catalog.digest().to_owned(),
        artifact_sha256: executable_sha256,
        artifact_size: payload.executable_size(),
        platform,
        artifact_arch,
        allowed_origins,
        payload: executable,
        payload_identity: identity,
        catalog_artifact: bound.clone(),
        platform_signature: bound.clone(),
        provenance: bound.clone(),
        offline_self_test: bound,
        authority: CandidateAuthority::Production,
        _derived_payload: Some(payload),
        installed_evidence: Some(installed_evidence),
    };
    candidate
        .validate(platform, architecture())
        .map_err(|_| CompositionError::CandidateRejected)?;
    Ok(candidate)
}

fn verify_independent_gates(
    payload: &mut VerifiedExecutablePayload,
    expected: &ExpectedRelease<'_>,
    verifier: &mut impl ReleaseCandidateVerifier,
) -> Result<(), CompositionError> {
    // Revalidate and rewind before each phase; rehash immediately afterward.
    // Signature and provenance must pass before executing even an offline test.
    let mut handle = payload
        .verification_handle()
        .map_err(|_| CompositionError::PayloadChanged)?;
    verifier
        .platform_signature(&mut handle, expected)
        .map_err(|_| CompositionError::PlatformSignatureRejected)?;
    let mut handle = payload
        .verification_handle()
        .map_err(|_| CompositionError::PayloadChanged)?;
    verifier
        .provenance(&mut handle, expected)
        .map_err(|_| CompositionError::ProvenanceRejected)?;
    let mut handle = payload
        .verification_handle()
        .map_err(|_| CompositionError::PayloadChanged)?;
    let embedded = verifier
        .embedded_identity(&mut handle, expected)
        .map_err(|_| CompositionError::EmbeddedIdentityRejected)?;
    if embedded.version != expected.version
        || embedded.platform != expected.platform
        || embedded.artifact_arch != expected.artifact_arch
        || embedded.native_host != "io.agentzero.browser_bridge"
        || embedded.protocol_version != crate::rpc::CONTRACT_VERSION
        || embedded.self_test_contract != crate::SELF_TEST_CONTRACT
        || embedded.install_contract != crate::INSTALL_CONTRACT
    {
        return Err(CompositionError::EmbeddedIdentityRejected);
    }
    let mut handle = payload
        .verification_handle()
        .map_err(|_| CompositionError::PayloadChanged)?;
    let report = verifier
        .offline_self_test(&mut handle, expected)
        .map_err(|_| CompositionError::SelfTestRejected)?;
    validate_self_test(&report, expected.version)?;
    payload
        .verify_retained()
        .map_err(|_| CompositionError::PayloadChanged)
}

fn validate_self_test(bytes: &[u8], version: &str) -> Result<(), CompositionError> {
    if bytes.len() > 2048 {
        return Err(CompositionError::SelfTestRejected);
    }
    let value = json::parse(bytes).map_err(|_| CompositionError::SelfTestRejected)?;
    let object = value
        .as_object()
        .ok_or(CompositionError::SelfTestRejected)?;
    let expected = [
        ("contract", crate::SELF_TEST_CONTRACT),
        ("companion_version", version),
        ("state", "passed"),
        ("framing", "passed"),
        ("registry", "passed"),
        ("path_discovery", "passed"),
        ("reason_code", "SELF_TEST_PASSED"),
    ];
    if object.len() != 9
        || expected
            .iter()
            .any(|(key, value)| object.get(*key).and_then(Value::as_str) != Some(*value))
        || object.get("schema_version").and_then(Value::as_u64) != Some(1)
        || object.get("install_ready") != Some(&Value::Bool(true))
    {
        return Err(CompositionError::SelfTestRejected);
    }
    Ok(())
}

/// Concrete Linux adapter for the reviewed companion's network-free self-test.
/// Call only after signature/provenance/static identity verification. Execute
/// the inherited descriptor, never a caller-selected or staging pathname.
/// Ambient credentials/proxies/loader options are absent from the environment.
/// macOS has no supported fd-exec equivalent: its `/dev/fd` character devices
/// cannot substitute for Linux procfs executable links. Do not fall back to
/// reopening a staging pathname and claim that this preserves handle identity.
#[cfg(target_os = "linux")]
pub(crate) fn run_linux_offline_self_test(executable: &File) -> Result<Vec<u8>, CompositionError> {
    use std::io::{ErrorKind, Read};
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    unsafe extern "C" {
        fn fcntl(fd: i32, command: i32, ...) -> i32;
    }
    const F_GETFD: i32 = 1;
    const F_SETFD: i32 = 2;
    const F_GETFL: i32 = 3;
    const F_SETFL: i32 = 4;
    const FD_CLOEXEC: i32 = 1;
    const O_NONBLOCK: i32 = 2048;

    let paths =
        crate::platform::discover_user_paths().map_err(|_| CompositionError::SelfTestRejected)?;
    let home = paths.home_root.ok_or(CompositionError::SelfTestRejected)?;
    let descriptor = executable.as_raw_fd();
    let mut command = Command::new(format!("/proc/self/fd/{descriptor}"));
    command
        .args(["self-test", "--json"])
        .env_clear()
        .env("HOME", home)
        .env("LC_ALL", "C")
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    // SAFETY: The child only invokes async-signal-safe fcntl before exec. The
    // borrowed File remains live through spawn; close-on-exec is changed only
    // in the child descriptor table, not in the parent or any other thread.
    unsafe {
        command.pre_exec(move || {
            let flags = fcntl(descriptor, F_GETFD);
            if flags < 0 || fcntl(descriptor, F_SETFD, flags & !FD_CLOEXEC) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .map_err(|_| CompositionError::SelfTestRejected)?;
    let result = (|| {
        let mut stdout = child
            .stdout
            .take()
            .ok_or(CompositionError::SelfTestRejected)?;
        // SAFETY: stdout owns a valid pipe descriptor. Make only its parent
        // read end nonblocking so an inherited pipe cannot defeat the deadline.
        unsafe {
            let flags = fcntl(stdout.as_raw_fd(), F_GETFL);
            if flags < 0 || fcntl(stdout.as_raw_fd(), F_SETFL, flags | O_NONBLOCK) < 0 {
                return Err(CompositionError::SelfTestRejected);
            }
        }
        let deadline = Instant::now() + Duration::from_secs(8);
        let mut output = Vec::new();
        let mut buffer = [0; 2049];
        loop {
            loop {
                match stdout.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) if output.len() + count <= 2048 => {
                        output.extend_from_slice(&buffer[..count])
                    }
                    Ok(_) => return Err(CompositionError::SelfTestRejected),
                    Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                    Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                    Err(_) => return Err(CompositionError::SelfTestRejected),
                }
            }
            if let Some(status) = child
                .try_wait()
                .map_err(|_| CompositionError::SelfTestRejected)?
            {
                if !status.success() {
                    return Err(CompositionError::SelfTestRejected);
                }
                // A final bounded drain covers bytes written between the last
                // read and child exit. Never wait for descendants holding stdout.
                loop {
                    match stdout.read(&mut buffer) {
                        Ok(0) => break,
                        Ok(count) if output.len() + count <= 2048 => {
                            output.extend_from_slice(&buffer[..count])
                        }
                        Ok(_) => return Err(CompositionError::SelfTestRejected),
                        Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                        Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                        Err(_) => return Err(CompositionError::SelfTestRejected),
                    }
                }
                return Ok(output);
            }
            if Instant::now() >= deadline {
                return Err(CompositionError::SelfTestRejected);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::{Read, Seek, SeekFrom};

    struct Verifier {
        calls: Vec<&'static str>,
        fail: Option<&'static str>,
        mutate: Option<std::path::PathBuf>,
    }

    impl Verifier {
        fn phase(&mut self, file: &mut File, phase: &'static str) -> Result<(), CompositionError> {
            self.calls.push(phase);
            // Each phase gets the exact same bytes, positioned at the start.
            let mut bytes = Vec::new();
            file.read_to_end(&mut bytes).unwrap();
            assert_eq!(bytes, b"fixture executable bytes");
            file.seek(SeekFrom::Start(3)).unwrap();
            if phase == "signature" {
                if let Some(path) = &self.mutate {
                    use std::os::unix::fs::PermissionsExt;
                    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
                    std::fs::write(path, b"fixture Executable bytes").unwrap();
                }
            }
            if self.fail == Some(phase) {
                Err(CompositionError::CandidateRejected)
            } else {
                Ok(())
            }
        }
    }

    impl ReleaseCandidateVerifier for Verifier {
        fn platform_signature(
            &mut self,
            file: &mut File,
            _: &ExpectedRelease<'_>,
        ) -> Result<(), CompositionError> {
            self.phase(file, "signature")
        }
        fn provenance(
            &mut self,
            file: &mut File,
            _: &ExpectedRelease<'_>,
        ) -> Result<(), CompositionError> {
            self.phase(file, "provenance")
        }
        fn embedded_identity(
            &mut self,
            file: &mut File,
            expected: &ExpectedRelease<'_>,
        ) -> Result<EmbeddedIdentity, CompositionError> {
            self.phase(file, "identity")?;
            Ok(EmbeddedIdentity {
                version: expected.version.to_owned(),
                platform: expected.platform,
                artifact_arch: expected.artifact_arch.to_owned(),
                native_host: "io.agentzero.browser_bridge".to_owned(),
                protocol_version: 1,
                self_test_contract: crate::SELF_TEST_CONTRACT.to_owned(),
                install_contract: crate::INSTALL_CONTRACT.to_owned(),
            })
        }
        fn offline_self_test(
            &mut self,
            file: &mut File,
            expected: &ExpectedRelease<'_>,
        ) -> Result<Vec<u8>, CompositionError> {
            self.phase(file, "self-test")?;
            Ok(report(expected.version))
        }
    }

    fn report(version: &str) -> Vec<u8> {
        format!(r#"{{"contract":"a0.browser-bridge.self-test.v1","schema_version":1,"companion_version":"{version}","state":"passed","framing":"passed","registry":"passed","path_discovery":"passed","install_ready":true,"reason_code":"SELF_TEST_PASSED"}}"#).into_bytes()
    }

    #[test]
    fn independent_gates_rehash_and_rewind_each_handle_before_execution() {
        for failure in [
            None,
            Some("signature"),
            Some("provenance"),
            Some("identity"),
            Some("self-test"),
            Some("mutate"),
        ] {
            let (parent, mut payload, path) = crate::release_payload::tests::composition_fixture();
            let archive_sha = payload.archive_sha256().to_owned();
            let executable_sha = payload.executable_sha256().to_owned();
            let expected = ExpectedRelease {
                version: "2.12.0",
                catalog_key_id: "fixture",
                catalog_sha256: &"1".repeat(64),
                archive_sha256: &archive_sha,
                executable_sha256: &executable_sha,
                executable_size: payload.executable_size(),
                platform: Platform::Linux,
                artifact_arch: "x86_64",
            };
            let mut verifier = Verifier {
                calls: Vec::new(),
                fail: failure,
                mutate: if failure == Some("mutate") {
                    Some(path)
                } else {
                    None
                },
            };
            let result = verify_independent_gates(&mut payload, &expected, &mut verifier);
            let (wanted, count) = match failure {
                None => (Ok(()), 4),
                Some("signature") => (Err(CompositionError::PlatformSignatureRejected), 1),
                Some("provenance") => (Err(CompositionError::ProvenanceRejected), 2),
                Some("identity") => (Err(CompositionError::EmbeddedIdentityRejected), 3),
                Some("self-test") => (Err(CompositionError::SelfTestRejected), 4),
                _ => (Err(CompositionError::PayloadChanged), 1),
            };
            assert_eq!(result, wanted);
            assert_eq!(verifier.calls.len(), count);
            drop(payload);
            std::fs::remove_dir_all(parent).unwrap();
        }
    }

    #[test]
    fn self_test_requires_exact_version_schema_and_real_install_readiness() {
        let good = report("2.12.0");
        assert_eq!(validate_self_test(&good, "2.12.0"), Ok(()));
        assert_eq!(
            validate_self_test(&good, "2.13.0"),
            Err(CompositionError::SelfTestRejected)
        );
        for bad in [
            String::from_utf8(good.clone())
                .unwrap()
                .replace("true", "false"),
            String::from_utf8(good.clone())
                .unwrap()
                .replace("SELF_TEST_PASSED", "RELEASE_TRUST_NOT_CONFIGURED"),
            String::from_utf8(good.clone()).unwrap().replace(
                "\"schema_version\":1",
                "\"schema_version\":1,\"extra\":true",
            ),
            String::from_utf8(good).unwrap().replace(
                "\"schema_version\":1",
                "\"schema_version\":1,\"schema_version\":1",
            ),
            " ".repeat(2049),
        ] {
            assert_eq!(
                validate_self_test(bad.as_bytes(), "2.12.0"),
                Err(CompositionError::SelfTestRejected)
            );
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn concrete_runner_executes_an_inherited_descriptor_without_path_or_shell_lookup() {
        let file = File::open("/usr/bin/true").unwrap();
        let output = run_linux_offline_self_test(&file).unwrap();
        assert!(output.is_empty());
        // Successful process exit alone is never companion identity/readiness.
        assert_eq!(
            validate_self_test(&output, "2.12.0"),
            Err(CompositionError::SelfTestRejected)
        );
        let file = File::open("/usr/bin/false").unwrap();
        assert_eq!(
            run_linux_offline_self_test(&file),
            Err(CompositionError::SelfTestRejected)
        );
    }
}
