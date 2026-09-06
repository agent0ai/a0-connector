//! Exact extraction proof for signed companion payload archives.
//!
//! A catalog-authenticated payload remains an archive. This module consumes the
//! exact retained archive handle and its unforgeable catalog proof, hashes the
//! compressed bytes while decoding one strictly bounded `.tar.gz` member, and
//! writes the one expected companion executable to generated private staging.
//! The resulting object retains both file handles and identities. It is not
//! install authority: platform signature, provenance, offline self-test, and
//! production release policy are independent mandatory gates.

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use flate2::bufread::GzDecoder;
use sha2::{Digest, Sha256};

use crate::release::catalog::VerifiedArtifactBytes;

#[cfg(target_os = "windows")]
#[path = "release_payload_windows.rs"]
mod windows;

pub const MAX_EXECUTABLE_BYTES: u64 = 512 * 1024 * 1024;

const TAR_BLOCK_BYTES: usize = 512;
const MAX_TAR_ZERO_BLOCKS: usize = 64;
const MAX_EXPANSION_RATIO: u64 = 64;
const MIN_DECOMPRESSED_LIMIT: u64 = 1024 * 1024;
const MAX_DECOMPRESSED_OVERHEAD: u64 = (MAX_TAR_ZERO_BLOCKS as u64 + 2) * 512;
const COPY_BUFFER_BYTES: usize = 32 * 1024;
const RANDOM_NAME_BYTES: usize = 16;
const CREATE_ATTEMPTS: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReleasePayloadError {
    InvalidCatalogProof,
    UnsupportedArchive,
    ArchiveHandleChanged,
    InvalidArchive,
    ArchiveExpansionLimit,
    PrivateStagingUnavailable,
    StagingIo,
    ExecutableHandleChanged,
}

impl ReleasePayloadError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::InvalidCatalogProof => "PAYLOAD_CATALOG_PROOF_INVALID",
            Self::UnsupportedArchive => "PAYLOAD_ARCHIVE_UNSUPPORTED",
            Self::ArchiveHandleChanged => "PAYLOAD_ARCHIVE_HANDLE_CHANGED",
            Self::InvalidArchive => "PAYLOAD_ARCHIVE_INVALID",
            Self::ArchiveExpansionLimit => "PAYLOAD_ARCHIVE_EXPANSION_LIMIT",
            Self::PrivateStagingUnavailable => "PAYLOAD_PRIVATE_STAGING_UNAVAILABLE",
            Self::StagingIo => "PAYLOAD_STAGING_IO_ERROR",
            Self::ExecutableHandleChanged => "PAYLOAD_EXECUTABLE_HANDLE_CHANGED",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct FileIdentity {
    device: u64,
    inode: u64,
    length: u64,
}

/// Pathless proof that one exact executable was deterministically derived from
/// one exact signed payload archive.
///
/// Dropping the proof removes only the generated private staging file and
/// directory if their retained identities still match. The archive is
/// caller-owned and is never deleted by this object.
pub struct VerifiedExecutablePayload {
    catalog_proof: VerifiedArtifactBytes,
    archive: Option<File>,
    archive_identity: FileIdentity,
    executable: Option<File>,
    executable_identity: FileIdentity,
    executable_sha256: String,
    executable_size: u64,
    staging_root: PathBuf,
    staging_root_identity: FileIdentity,
    staging_file: PathBuf,
    #[cfg(target_os = "windows")]
    windows_guards: Vec<File>,
    #[cfg(target_os = "windows")]
    download_staging: Option<StagingGuard>,
}

impl std::fmt::Debug for VerifiedExecutablePayload {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("VerifiedExecutablePayload")
            .field("platform", &self.platform())
            .field("arch", &self.arch())
            .field("archive_size", &self.catalog_proof.artifact().size)
            .field("executable_size", &self.executable_size)
            .finish_non_exhaustive()
    }
}

impl VerifiedExecutablePayload {
    #[cfg(target_os = "macos")]
    pub(crate) fn macos_verification_lease(
        &mut self,
    ) -> Result<MacosVerificationLease, ReleasePayloadError> {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::OpenOptionsExt;
        unsafe extern "C" {
            fn flock(fd: i32, operation: i32) -> i32;
        }
        self.verify_retained()?;
        verify_macos_parent_chain(&self.staging_root)?;
        let executable = self
            .executable
            .as_ref()
            .ok_or(ReleasePayloadError::ExecutableHandleChanged)?
            .try_clone()
            .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?;
        let lock_path = self.staging_root.join(".verification.lock");
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&lock_path)
            .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
        let lock_identity = file_identity(&lock)?;
        let lease = MacosVerificationLease {
            path: self.staging_file.clone(),
            root: self.staging_root.clone(),
            root_identity: self.staging_root_identity.clone(),
            executable,
            identity: self.executable_identity.clone(),
            digest: self.executable_sha256.clone(),
            lock,
            lock_path,
            lock_identity,
        };
        // LOCK_EX | LOCK_NB. The create-only lock also prevents parallel leases
        // from being minted for this staging inode; no installed state is touched.
        if unsafe { flock(lease.lock.as_raw_fd(), 2 | 4) } != 0 {
            return Err(ReleasePayloadError::PrivateStagingUnavailable);
        }
        Ok(lease)
    }

    /// Give only trusted in-crate release verifiers a duplicate of the retained
    /// read-only handle. The duplicate shares the file object, never a path;
    /// callers must revalidate this proof after each verifier returns.
    pub(crate) fn verification_handle(&mut self) -> Result<File, ReleasePayloadError> {
        self.verify_retained()?;
        self.executable
            .as_ref()
            .ok_or(ReleasePayloadError::ExecutableHandleChanged)?
            .try_clone()
            .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)
    }

    pub fn catalog_proof(&self) -> &VerifiedArtifactBytes {
        &self.catalog_proof
    }

    pub fn platform(&self) -> &str {
        &self.catalog_proof.artifact().platform
    }

    pub fn arch(&self) -> &str {
        &self.catalog_proof.artifact().arch
    }

    pub fn archive_sha256(&self) -> &str {
        &self.catalog_proof.artifact().sha256
    }

    pub fn archive_size(&self) -> u64 {
        self.catalog_proof.artifact().size
    }

    pub fn executable_sha256(&self) -> &str {
        &self.executable_sha256
    }

    pub const fn executable_size(&self) -> u64 {
        self.executable_size
    }

    /// Re-hash both retained handles. This is the handoff check for future
    /// platform-signature, provenance, self-test, and install-candidate gates.
    pub fn verify_retained(&mut self) -> Result<(), ReleasePayloadError> {
        let archive = self
            .archive
            .as_mut()
            .ok_or(ReleasePayloadError::ArchiveHandleChanged)?;
        if file_identity(archive)? != self.archive_identity {
            return Err(ReleasePayloadError::ArchiveHandleChanged);
        }
        let archive_digest = hash_retained(
            archive,
            self.catalog_proof.artifact().size,
            ReleasePayloadError::ArchiveHandleChanged,
        )?;
        if archive_digest != self.catalog_proof.artifact().sha256
            || file_identity(archive)? != self.archive_identity
        {
            return Err(ReleasePayloadError::ArchiveHandleChanged);
        }

        let executable = self
            .executable
            .as_mut()
            .ok_or(ReleasePayloadError::ExecutableHandleChanged)?;
        if file_identity(executable).map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?
            != self.executable_identity
        {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        let executable_digest = hash_retained(
            executable,
            self.executable_size,
            ReleasePayloadError::ExecutableHandleChanged,
        )?;
        if executable_digest != self.executable_sha256
            || file_identity(executable)
                .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?
                != self.executable_identity
        {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        Ok(())
    }

    /// Copy only from the retained verified executable handle. This never
    /// reopens an archive-selected or caller-selected executable path.
    pub fn copy_executable_to<W: Write>(
        &mut self,
        destination: &mut W,
    ) -> Result<(), ReleasePayloadError> {
        self.verify_retained()?;
        let executable = self
            .executable
            .as_mut()
            .ok_or(ReleasePayloadError::ExecutableHandleChanged)?;
        executable
            .seek(SeekFrom::Start(0))
            .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?;
        let result = copy_exact(
            executable,
            destination,
            self.executable_size,
            &self.executable_sha256,
        );
        let rewind = executable.seek(SeekFrom::Start(0));
        result?;
        rewind.map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?;
        if file_identity(executable).map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?
            != self.executable_identity
        {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        Ok(())
    }
}

/// Darwin's public code-signing and exec APIs consume paths, not retained file
/// descriptors. This lease permits exactly the generated private staging path
/// while retaining, hashing and checking its original executable inode.
#[cfg(target_os = "macos")]
pub(crate) struct MacosVerificationLease {
    path: PathBuf,
    root: PathBuf,
    root_identity: FileIdentity,
    executable: File,
    identity: FileIdentity,
    digest: String,
    lock: File,
    lock_path: PathBuf,
    lock_identity: FileIdentity,
}

#[cfg(target_os = "macos")]
impl MacosVerificationLease {
    pub(crate) fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn verify(&mut self, supplied: &File) -> Result<(), ReleasePayloadError> {
        use std::os::unix::fs::MetadataExt;
        unsafe extern "C" {
            fn getuid() -> u32;
        }
        let uid = unsafe { getuid() };
        verify_macos_parent_chain(&self.root)?;
        if !path_identity(&self.root)?
            .is_some_and(|identity| same_object(&identity, &self.root_identity))
            || !path_identity(&self.path)?.is_some_and(|identity| identity == self.identity)
            || file_identity(supplied)? != self.identity
            || file_identity(&self.executable)? != self.identity
            || !path_identity(&self.lock_path)?
                .is_some_and(|identity| identity == self.lock_identity)
        {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        let metadata = self
            .executable
            .metadata()
            .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?;
        let lock_metadata = self
            .lock
            .metadata()
            .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
        if lock_metadata.uid() != uid
            || lock_metadata.nlink() != 1
            || lock_metadata.mode() & 0o077 != 0
        {
            return Err(ReleasePayloadError::PrivateStagingUnavailable);
        }
        if metadata.uid() != unsafe { getuid() }
            || metadata.nlink() != 1
            || metadata.mode() & 0o777 != 0o500
            || hash_retained(
                &mut self.executable,
                self.identity.length,
                ReleasePayloadError::ExecutableHandleChanged,
            )? != self.digest
        {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        let after = self
            .executable
            .metadata()
            .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?;
        if metadata.mtime_nsec() != after.mtime_nsec()
            || metadata.mtime() != after.mtime()
            || metadata.ctime_nsec() != after.ctime_nsec()
            || metadata.ctime() != after.ctime()
            || !path_identity(&self.path)?.is_some_and(|identity| identity == self.identity)
        {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
impl Drop for MacosVerificationLease {
    fn drop(&mut self) {
        if path_identity(&self.lock_path)
            .ok()
            .flatten()
            .is_some_and(|identity| identity == self.lock_identity)
        {
            let _ = fs::remove_file(&self.lock_path);
        }
        if path_identity(&self.root)
            .ok()
            .flatten()
            .is_some_and(|identity| same_object(&identity, &self.root_identity))
        {
            let _ = fs::remove_dir(&self.root); // succeeds only if payload cleanup already made it empty
        }
        // Closing the retained descriptor releases its advisory lock.
    }
}

#[cfg(target_os = "macos")]
fn verify_macos_parent_chain(root: &Path) -> Result<(), ReleasePayloadError> {
    use std::os::unix::fs::MetadataExt;
    unsafe extern "C" {
        fn getuid() -> u32;
    }
    let uid = unsafe { getuid() };
    for (index, parent) in root.ancestors().enumerate() {
        let metadata = fs::symlink_metadata(parent)
            .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
        let private = index <= 1;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || (metadata.uid() != uid && (private || metadata.uid() != 0))
            || (private && metadata.mode() & 0o077 != 0)
            || (!private
                && metadata.mode() & 0o022 != 0
                && !(metadata.uid() == 0 && metadata.mode() & 0o1000 != 0))
        {
            return Err(ReleasePayloadError::PrivateStagingUnavailable);
        }
    }
    Ok(())
}

impl Drop for VerifiedExecutablePayload {
    fn drop(&mut self) {
        let owned = self
            .executable
            .as_ref()
            .and_then(|file| file_identity(file).ok())
            .is_some_and(|identity| same_object(&identity, &self.executable_identity));
        // Windows denies delete sharing while these exact handles are live.
        // Close them deliberately before identity-checked owned cleanup.
        drop(self.executable.take());
        drop(self.archive.take());
        #[cfg(target_os = "windows")]
        self.windows_guards.clear();
        if owned
            && path_identity(&self.staging_file)
                .ok()
                .flatten()
                .is_some_and(|identity| same_object(&identity, &self.executable_identity))
        {
            let _ = fs::remove_file(&self.staging_file);
        }
        if path_identity(&self.staging_root)
            .ok()
            .flatten()
            .is_some_and(|identity| same_object(&identity, &self.staging_root_identity))
        {
            let _ = fs::remove_dir(&self.staging_root);
        }
    }
}

/// Extract a single catalog-authenticated `.tar.gz` payload into generated
/// private staging. Windows uses the same protected current-user/SYSTEM DACL
/// and retained non-reparse handle guarantees as the private artifact spool.
pub(crate) fn download_verified_payload(
    catalog: &crate::release::catalog::VerifiedCatalog,
    artifact_name: &str,
    private_parent: &Path,
) -> Result<VerifiedExecutablePayload, ReleasePayloadError> {
    let parent = private_staging_parent(private_parent)?;
    let (root, identity) = create_private_root(&parent)?;
    let mut guard = StagingGuard::new(root, identity)?;
    let (path, mut archive) = create_private_file(&guard.root)?;
    guard.file = Some((path.clone(), file_identity(&archive)?));
    let result = catalog.download_artifact(artifact_name, &mut archive);
    // Cleanup ownership follows the same inode after bounded download changes
    // its length; no caller pathname is retained as authority.
    guard.file = Some((path.clone(), file_identity(&archive)?));
    let proof = result.map_err(|_| ReleasePayloadError::InvalidCatalogProof)?;
    archive
        .sync_all()
        .map_err(|_| ReleasePayloadError::StagingIo)?;
    let retained = file_identity(&archive)?;
    #[cfg(target_os = "windows")]
    let readonly = windows::seal(archive, &path)?;
    #[cfg(not(target_os = "windows"))]
    let readonly = File::open(&path).map_err(|_| ReleasePayloadError::StagingIo)?;
    if file_identity(&readonly)? != retained {
        return Err(ReleasePayloadError::ArchiveHandleChanged);
    }
    #[cfg(not(target_os = "windows"))]
    drop(archive);
    // Extraction and verification keep the original read-only archive handle.
    // Dropping the private download guard unlinks only its exact owned inode.
    let payload = extract_verified_payload(proof, readonly, &parent)?;
    #[cfg(target_os = "windows")]
    let payload = {
        let mut payload = payload;
        // Unlike Unix, the archive cannot be unlinked while its deny-delete
        // handle is retained. Keep its generated cleanup owner with the proof.
        payload.download_staging = Some(guard);
        payload
    };
    Ok(payload)
}

pub fn extract_verified_payload(
    proof: VerifiedArtifactBytes,
    mut archive: File,
    private_parent: &Path,
) -> Result<VerifiedExecutablePayload, ReleasePayloadError> {
    validate_catalog_proof(&proof)?;
    #[cfg(target_os = "windows")]
    let (mut archive, mut archive_guards) = windows::retain_archive(archive)?;
    let expected_name = expected_executable_name(&proof)?;
    let archive_identity = file_identity(&archive)?;
    if archive_identity.length != proof.artifact().size {
        return Err(ReleasePayloadError::ArchiveHandleChanged);
    }
    archive
        .seek(SeekFrom::Start(0))
        .map_err(|_| ReleasePayloadError::ArchiveHandleChanged)?;

    let parent = private_staging_parent(private_parent)?;
    let (root, root_identity) = create_private_root(&parent)?;
    let mut guard = StagingGuard::new(root, root_identity)?;
    let (staging_file, mut executable) = create_private_file(&guard.root)?;
    guard.file = Some((
        staging_file.clone(),
        file_identity(&executable).map_err(|_| ReleasePayloadError::StagingIo)?,
    ));

    let archive_size = proof.artifact().size;
    let decompressed_limit = archive_size
        .saturating_mul(MAX_EXPANSION_RATIO)
        .max(MIN_DECOMPRESSED_LIMIT)
        .min(MAX_EXECUTABLE_BYTES + MAX_DECOMPRESSED_OVERHEAD);
    let hashing = HashingReader::new(archive, archive_size);
    let buffered = BufReader::with_capacity(COPY_BUFFER_BYTES, hashing);
    let mut decoder = GzDecoder::new(buffered);
    let (executable_sha256, executable_size) = extract_tar(
        &mut decoder,
        &mut executable,
        expected_name,
        decompressed_limit,
    )?;
    let mut buffered = decoder.into_inner();
    if !buffered
        .fill_buf()
        .map_err(|_| ReleasePayloadError::InvalidArchive)?
        .is_empty()
    {
        return Err(ReleasePayloadError::InvalidArchive);
    }
    let hashing = buffered.into_inner();
    let (mut archive, archive_sha256, consumed_archive_size) = hashing.finish();
    if consumed_archive_size != archive_size || archive_sha256 != proof.artifact().sha256 {
        return Err(ReleasePayloadError::ArchiveHandleChanged);
    }
    if file_identity(&archive)? != archive_identity {
        return Err(ReleasePayloadError::ArchiveHandleChanged);
    }
    archive
        .seek(SeekFrom::Start(0))
        .map_err(|_| ReleasePayloadError::ArchiveHandleChanged)?;

    executable
        .sync_all()
        .map_err(|_| ReleasePayloadError::StagingIo)?;
    set_private_executable_mode(&staging_file)?;
    verify_private_executable_mode(&staging_file)?;
    let written_identity =
        file_identity(&executable).map_err(|_| ReleasePayloadError::StagingIo)?;
    #[cfg(target_os = "windows")]
    let mut retained_executable = windows::seal(executable, &staging_file)?;
    #[cfg(not(target_os = "windows"))]
    let mut retained_executable =
        File::open(&staging_file).map_err(|_| ReleasePayloadError::StagingIo)?;
    let executable_identity =
        file_identity(&retained_executable).map_err(|_| ReleasePayloadError::StagingIo)?;
    if executable_identity != written_identity
        || path_identity(&staging_file)? != Some(executable_identity.clone())
    {
        return Err(ReleasePayloadError::StagingIo);
    }
    #[cfg(not(target_os = "windows"))]
    drop(executable);
    retained_executable
        .seek(SeekFrom::Start(0))
        .map_err(|_| ReleasePayloadError::StagingIo)?;
    if executable_identity.length != executable_size {
        return Err(ReleasePayloadError::StagingIo);
    }
    #[cfg(target_os = "windows")]
    if hash_retained(
        &mut retained_executable,
        executable_size,
        ReleasePayloadError::ExecutableHandleChanged,
    )? != executable_sha256
    {
        // Closing the writer before acquiring Windows deny-write sharing is a
        // narrow transition; bind the sealed bytes before minting the proof.
        return Err(ReleasePayloadError::ExecutableHandleChanged);
    }
    guard.disarm();
    #[cfg(target_os = "windows")]
    archive_guards.append(&mut guard.windows_guards);

    Ok(VerifiedExecutablePayload {
        catalog_proof: proof,
        archive: Some(archive),
        archive_identity,
        executable: Some(retained_executable),
        executable_identity,
        executable_sha256,
        executable_size,
        staging_root: guard.root.clone(),
        staging_root_identity: guard.root_identity.clone(),
        staging_file,
        #[cfg(target_os = "windows")]
        windows_guards: archive_guards,
        #[cfg(target_os = "windows")]
        download_staging: None,
    })
}

fn validate_catalog_proof(proof: &VerifiedArtifactBytes) -> Result<(), ReleasePayloadError> {
    let artifact = proof.artifact();
    if artifact.kind != "payload"
        || artifact.size == 0
        || artifact.size > MAX_EXECUTABLE_BYTES
        || !valid_sha256(&artifact.sha256)
        || !valid_sha256(proof.catalog_digest())
        || !matches!(
            (artifact.platform.as_str(), artifact.arch.as_str()),
            ("macos", "universal2")
                | ("windows", "x86_64" | "arm64")
                | ("linux", "x86_64" | "aarch64")
        )
    {
        return Err(ReleasePayloadError::InvalidCatalogProof);
    }
    if !artifact.name.ends_with(".tar.gz") || artifact.name.len() <= ".tar.gz".len() {
        return Err(ReleasePayloadError::UnsupportedArchive);
    }
    Ok(())
}

fn expected_executable_name(
    proof: &VerifiedArtifactBytes,
) -> Result<&'static [u8], ReleasePayloadError> {
    match proof.artifact().platform.as_str() {
        "macos" | "linux" => Ok(b"a0-browser-bridge"),
        "windows" => Ok(b"a0-browser-bridge.exe"),
        _ => Err(ReleasePayloadError::InvalidCatalogProof),
    }
}

fn extract_tar<R: Read, W: Write>(
    source: &mut R,
    destination: &mut W,
    expected_name: &[u8],
    decompressed_limit: u64,
) -> Result<(String, u64), ReleasePayloadError> {
    let mut observed = 0u64;
    let mut header = [0u8; TAR_BLOCK_BYTES];
    read_exact_bounded(source, &mut header, &mut observed, decompressed_limit)?;
    validate_tar_header(&header, expected_name)?;
    let executable_size = parse_tar_octal(&header[124..136])
        .filter(|size| *size > 0 && *size <= MAX_EXECUTABLE_BYTES)
        .ok_or(ReleasePayloadError::InvalidArchive)?;
    let padding = (TAR_BLOCK_BYTES as u64 - executable_size % TAR_BLOCK_BYTES as u64)
        % TAR_BLOCK_BYTES as u64;
    let minimum_stream_size =
        TAR_BLOCK_BYTES as u64 + executable_size + padding + 2 * TAR_BLOCK_BYTES as u64;
    if minimum_stream_size > decompressed_limit {
        return Err(ReleasePayloadError::ArchiveExpansionLimit);
    }

    let mut hasher = Sha256::new();
    let mut remaining = executable_size;
    let mut buffer = [0u8; COPY_BUFFER_BYTES];
    while remaining > 0 {
        let take = usize::try_from(remaining.min(buffer.len() as u64))
            .map_err(|_| ReleasePayloadError::InvalidArchive)?;
        read_exact_bounded(
            source,
            &mut buffer[..take],
            &mut observed,
            decompressed_limit,
        )?;
        hasher.update(&buffer[..take]);
        destination
            .write_all(&buffer[..take])
            .map_err(|_| ReleasePayloadError::StagingIo)?;
        remaining -= take as u64;
    }

    if padding > 0 {
        let padding = usize::try_from(padding).expect("tar padding fits usize");
        read_exact_bounded(
            source,
            &mut buffer[..padding],
            &mut observed,
            decompressed_limit,
        )?;
        if buffer[..padding].iter().any(|byte| *byte != 0) {
            return Err(ReleasePayloadError::InvalidArchive);
        }
    }

    let mut zero_blocks = 0usize;
    loop {
        match read_block_or_eof(source, &mut header, &mut observed, decompressed_limit)? {
            false if zero_blocks >= 2 => break,
            false => return Err(ReleasePayloadError::InvalidArchive),
            true => {
                zero_blocks += 1;
                if zero_blocks > MAX_TAR_ZERO_BLOCKS || header.iter().any(|byte| *byte != 0) {
                    return Err(ReleasePayloadError::InvalidArchive);
                }
            }
        }
    }

    Ok((hex(&hasher.finalize()), executable_size))
}

fn validate_tar_header(
    header: &[u8; TAR_BLOCK_BYTES],
    expected_name: &[u8],
) -> Result<(), ReleasePayloadError> {
    let name = terminated_bytes(&header[0..100]).ok_or(ReleasePayloadError::InvalidArchive)?;
    let mode = parse_tar_octal(&header[100..108]).ok_or(ReleasePayloadError::InvalidArchive)?;
    let expected_checksum =
        parse_tar_octal(&header[148..156]).ok_or(ReleasePayloadError::InvalidArchive)?;
    let actual_checksum = header
        .iter()
        .enumerate()
        .map(|(index, byte)| {
            if (148..156).contains(&index) {
                b' ' as u64
            } else {
                *byte as u64
            }
        })
        .sum::<u64>();
    if name != expected_name
        || mode > 0o777
        || mode & 0o500 != 0o500
        || expected_checksum != actual_checksum
        || !matches!(header[156], 0 | b'0')
        || header[157..257].iter().any(|byte| *byte != 0)
        || &header[257..263] != b"ustar\0"
        || &header[263..265] != b"00"
        || header[329..345].iter().any(|byte| *byte != 0)
        || header[345..500].iter().any(|byte| *byte != 0)
        || header[500..512].iter().any(|byte| *byte != 0)
    {
        return Err(ReleasePayloadError::InvalidArchive);
    }
    Ok(())
}

fn terminated_bytes(field: &[u8]) -> Option<&[u8]> {
    let end = field
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(field.len());
    if field[end..].iter().any(|byte| *byte != 0) {
        return None;
    }
    Some(&field[..end])
}

fn parse_tar_octal(field: &[u8]) -> Option<u64> {
    if field.first().is_some_and(|byte| byte & 0x80 != 0) {
        return None;
    }
    let mut value = 0u64;
    let mut saw_digit = false;
    let mut terminated = false;
    for byte in field {
        match *byte {
            b'0'..=b'7' if !terminated => {
                saw_digit = true;
                value = value.checked_mul(8)?.checked_add((byte - b'0') as u64)?;
            }
            0 | b' ' => {
                if saw_digit {
                    terminated = true;
                }
            }
            _ => return None,
        }
    }
    saw_digit.then_some(value)
}

fn read_exact_bounded<R: Read>(
    reader: &mut R,
    output: &mut [u8],
    observed: &mut u64,
    limit: u64,
) -> Result<(), ReleasePayloadError> {
    *observed = observed
        .checked_add(output.len() as u64)
        .ok_or(ReleasePayloadError::ArchiveExpansionLimit)?;
    if *observed > limit {
        return Err(ReleasePayloadError::ArchiveExpansionLimit);
    }
    reader
        .read_exact(output)
        .map_err(|_| ReleasePayloadError::InvalidArchive)
}

fn read_block_or_eof<R: Read>(
    reader: &mut R,
    block: &mut [u8; TAR_BLOCK_BYTES],
    observed: &mut u64,
    limit: u64,
) -> Result<bool, ReleasePayloadError> {
    let first = reader
        .read(&mut block[..1])
        .map_err(|_| ReleasePayloadError::InvalidArchive)?;
    if first == 0 {
        return Ok(false);
    }
    *observed = observed
        .checked_add(TAR_BLOCK_BYTES as u64)
        .ok_or(ReleasePayloadError::ArchiveExpansionLimit)?;
    if *observed > limit {
        return Err(ReleasePayloadError::ArchiveExpansionLimit);
    }
    reader
        .read_exact(&mut block[1..])
        .map_err(|_| ReleasePayloadError::InvalidArchive)?;
    Ok(true)
}

struct HashingReader {
    file: File,
    hasher: Sha256,
    total: u64,
    expected: u64,
}

impl HashingReader {
    fn new(file: File, expected: u64) -> Self {
        Self {
            file,
            hasher: Sha256::new(),
            total: 0,
            expected,
        }
    }

    fn finish(self) -> (File, String, u64) {
        (self.file, hex(&self.hasher.finalize()), self.total)
    }
}

impl Read for HashingReader {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let remaining = self.expected.saturating_sub(self.total);
        if remaining == 0 {
            let mut probe = [0u8; 1];
            if self.file.read(&mut probe)? != 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "archive exceeds authenticated size",
                ));
            }
            return Ok(0);
        }
        let limit = usize::try_from(remaining.min(output.len() as u64))
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid archive size"))?;
        let read = self.file.read(&mut output[..limit])?;
        self.total += read as u64;
        self.hasher.update(&output[..read]);
        Ok(read)
    }
}

fn copy_exact<R: Read, W: Write>(
    source: &mut R,
    destination: &mut W,
    expected_size: u64,
    expected_sha256: &str,
) -> Result<(), ReleasePayloadError> {
    let mut hasher = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; COPY_BUFFER_BYTES];
    loop {
        let read = source
            .read(&mut buffer)
            .map_err(|_| ReleasePayloadError::ExecutableHandleChanged)?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or(ReleasePayloadError::ExecutableHandleChanged)?;
        if total > expected_size {
            return Err(ReleasePayloadError::ExecutableHandleChanged);
        }
        hasher.update(&buffer[..read]);
        destination
            .write_all(&buffer[..read])
            .map_err(|_| ReleasePayloadError::StagingIo)?;
    }
    if total != expected_size || hex(&hasher.finalize()) != expected_sha256 {
        return Err(ReleasePayloadError::ExecutableHandleChanged);
    }
    Ok(())
}

fn hash_retained(
    file: &mut File,
    expected_size: u64,
    failure: ReleasePayloadError,
) -> Result<String, ReleasePayloadError> {
    file.seek(SeekFrom::Start(0)).map_err(|_| failure)?;
    let mut hasher = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; COPY_BUFFER_BYTES];
    loop {
        let read = file.read(&mut buffer).map_err(|_| failure)?;
        if read == 0 {
            break;
        }
        total = total.checked_add(read as u64).ok_or(failure)?;
        if total > expected_size {
            return Err(failure);
        }
        hasher.update(&buffer[..read]);
    }
    file.seek(SeekFrom::Start(0)).map_err(|_| failure)?;
    if total != expected_size {
        return Err(failure);
    }
    Ok(hex(&hasher.finalize()))
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn same_object(left: &FileIdentity, right: &FileIdentity) -> bool {
    left.device == right.device && left.inode == right.inode
}

fn hex(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(&mut output, "{byte:02x}").expect("string formatting is infallible");
    }
    output
}

#[cfg(unix)]
fn file_identity(file: &File) -> Result<FileIdentity, ReleasePayloadError> {
    use std::os::unix::fs::MetadataExt;

    let metadata = file
        .metadata()
        .map_err(|_| ReleasePayloadError::ArchiveHandleChanged)?;
    if !metadata.is_file() {
        return Err(ReleasePayloadError::ArchiveHandleChanged);
    }
    Ok(FileIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        length: metadata.len(),
    })
}

#[cfg(target_os = "windows")]
fn file_identity(file: &File) -> Result<FileIdentity, ReleasePayloadError> {
    windows::identity(file, false)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn file_identity(_file: &File) -> Result<FileIdentity, ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(unix)]
fn path_identity(path: &Path) -> Result<Option<FileIdentity>, ReleasePayloadError> {
    use std::os::unix::fs::MetadataExt;

    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(ReleasePayloadError::StagingIo),
    };
    if metadata.file_type().is_symlink() || (!metadata.is_file() && !metadata.is_dir()) {
        return Err(ReleasePayloadError::StagingIo);
    }
    Ok(Some(FileIdentity {
        device: metadata.dev(),
        inode: metadata.ino(),
        length: metadata.len(),
    }))
}

#[cfg(target_os = "windows")]
fn path_identity(path: &Path) -> Result<Option<FileIdentity>, ReleasePayloadError> {
    windows::path_identity(path)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn path_identity(_path: &Path) -> Result<Option<FileIdentity>, ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(unix)]
fn private_staging_parent(requested: &Path) -> Result<PathBuf, ReleasePayloadError> {
    use std::os::unix::fs::PermissionsExt;

    if !requested.is_absolute() {
        return Err(ReleasePayloadError::PrivateStagingUnavailable);
    }
    let metadata = fs::symlink_metadata(requested)
        .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(ReleasePayloadError::PrivateStagingUnavailable);
    }
    let canonical = requested
        .canonicalize()
        .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    let canonical_metadata = fs::symlink_metadata(&canonical)
        .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    if canonical_metadata.file_type().is_symlink()
        || !canonical_metadata.is_dir()
        || canonical_metadata.permissions().mode() & 0o077 != 0
    {
        return Err(ReleasePayloadError::PrivateStagingUnavailable);
    }
    Ok(canonical)
}

#[cfg(target_os = "windows")]
fn private_staging_parent(requested: &Path) -> Result<PathBuf, ReleasePayloadError> {
    windows::parent(requested)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn private_staging_parent(_requested: &Path) -> Result<PathBuf, ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(unix)]
fn create_private_root(parent: &Path) -> Result<(PathBuf, FileIdentity), ReleasePayloadError> {
    use std::os::unix::fs::PermissionsExt;

    for _ in 0..CREATE_ATTEMPTS {
        let root = parent.join(random_name(".a0-browser-payload-")?);
        match fs::create_dir(&root) {
            Ok(()) => {
                if fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).is_err() {
                    let _ = fs::remove_dir(&root);
                    return Err(ReleasePayloadError::PrivateStagingUnavailable);
                }
                let canonical = root
                    .canonicalize()
                    .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
                if canonical.parent() != Some(parent) {
                    let _ = fs::remove_dir(&root);
                    return Err(ReleasePayloadError::PrivateStagingUnavailable);
                }
                let identity = path_identity(&canonical)?
                    .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?;
                return Ok((canonical, identity));
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(ReleasePayloadError::PrivateStagingUnavailable),
        }
    }
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(target_os = "windows")]
fn create_private_root(parent: &Path) -> Result<(PathBuf, FileIdentity), ReleasePayloadError> {
    windows::create_root(parent)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn create_private_root(_parent: &Path) -> Result<(PathBuf, FileIdentity), ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(unix)]
fn create_private_file(root: &Path) -> Result<(PathBuf, File), ReleasePayloadError> {
    use std::os::unix::fs::PermissionsExt;

    for _ in 0..CREATE_ATTEMPTS {
        let path = root.join(random_name(".executable-")?);
        match OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => {
                if fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).is_err() {
                    drop(file);
                    let _ = fs::remove_file(&path);
                    return Err(ReleasePayloadError::StagingIo);
                }
                return Ok((path, file));
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(ReleasePayloadError::StagingIo),
        }
    }
    Err(ReleasePayloadError::StagingIo)
}

#[cfg(target_os = "windows")]
fn create_private_file(root: &Path) -> Result<(PathBuf, File), ReleasePayloadError> {
    windows::create_file(root)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn create_private_file(_root: &Path) -> Result<(PathBuf, File), ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(unix)]
fn set_private_executable_mode(path: &Path) -> Result<(), ReleasePayloadError> {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(0o500))
        .map_err(|_| ReleasePayloadError::StagingIo)
}

#[cfg(unix)]
fn verify_private_executable_mode(path: &Path) -> Result<(), ReleasePayloadError> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::symlink_metadata(path).map_err(|_| ReleasePayloadError::StagingIo)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.permissions().mode() & 0o777 != 0o500
    {
        return Err(ReleasePayloadError::StagingIo);
    }
    Ok(())
}

#[cfg(target_os = "windows")]
fn set_private_executable_mode(path: &Path) -> Result<(), ReleasePayloadError> {
    // Windows executable privacy is its explicit protected DACL, not Unix mode
    // emulation or the DOS readonly bit. Creation already installed that DACL.
    windows::verify_private(path)
}

#[cfg(target_os = "windows")]
fn verify_private_executable_mode(path: &Path) -> Result<(), ReleasePayloadError> {
    windows::verify_private(path)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn set_private_executable_mode(_path: &Path) -> Result<(), ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

#[cfg(not(any(unix, target_os = "windows")))]
fn verify_private_executable_mode(_path: &Path) -> Result<(), ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

fn random_name(prefix: &str) -> Result<String, ReleasePayloadError> {
    let mut random = [0u8; RANDOM_NAME_BYTES];
    getrandom::fill(&mut random).map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    let mut output = String::with_capacity(prefix.len() + RANDOM_NAME_BYTES * 2);
    output.push_str(prefix);
    for byte in random {
        use std::fmt::Write as _;
        write!(&mut output, "{byte:02x}").expect("string formatting is infallible");
    }
    Ok(output)
}

struct StagingGuard {
    root: PathBuf,
    root_identity: FileIdentity,
    file: Option<(PathBuf, FileIdentity)>,
    active: bool,
    #[cfg(target_os = "windows")]
    windows_guards: Vec<File>,
}

impl StagingGuard {
    fn new(root: PathBuf, root_identity: FileIdentity) -> Result<Self, ReleasePayloadError> {
        let guard = Self {
            root,
            root_identity,
            file: None,
            active: true,
            #[cfg(target_os = "windows")]
            windows_guards: Vec::new(),
        };
        #[cfg(target_os = "windows")]
        let guard = {
            let mut guard = guard;
            guard.windows_guards = windows::guard_chain(&guard.root)?;
            guard
        };
        Ok(guard)
    }

    fn disarm(&mut self) {
        self.active = false;
    }
}

impl Drop for StagingGuard {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        #[cfg(target_os = "windows")]
        self.windows_guards.clear();
        if let Some(file) = &self.file {
            if path_identity(&file.0)
                .ok()
                .flatten()
                .is_some_and(|identity| same_object(&identity, &file.1))
            {
                let _ = fs::remove_file(&file.0);
            }
        }
        if path_identity(&self.root)
            .ok()
            .flatten()
            .is_some_and(|identity| same_object(&identity, &self.root_identity))
        {
            let _ = fs::remove_dir(&self.root);
        }
    }
}

/// BY_HANDLE_FILE_INFORMATION's stable volume/file-index identity. This pure
/// decoder is shared by the real Win32 adapter and host-compatible tests.
#[cfg(any(target_os = "windows", test))]
fn windows_identity_words(
    words: &[u32; 13],
    directory: bool,
) -> Result<FileIdentity, ReleasePayloadError> {
    if words[0] & 0x400 != 0
        || (words[0] & 0x10 != 0) != directory
        || (!directory && words[10] != 1)
    {
        return Err(ReleasePayloadError::PrivateStagingUnavailable);
    }
    Ok(FileIdentity {
        device: u64::from(words[7]),
        inode: (u64::from(words[11]) << 32) | u64::from(words[12]),
        length: (u64::from(words[8]) << 32) | u64::from(words[9]),
    })
}

#[cfg(test)]
mod windows_identity_tests {
    use super::*;
    #[test]
    fn windows_metadata_identity_binds_volume_index_and_rejects_reparse_links() {
        let mut words = [0u32; 13];
        words[7] = 42;
        words[8] = 1;
        words[9] = 2;
        words[10] = 1;
        words[11] = 3;
        words[12] = 4;
        let identity = windows_identity_words(&words, false).unwrap();
        assert_eq!(
            identity,
            FileIdentity {
                device: 42,
                inode: (3u64 << 32) | 4,
                length: (1u64 << 32) | 2
            }
        );
        words[10] = 2;
        assert!(windows_identity_words(&words, false).is_err());
        words[10] = 1;
        words[0] = 0x400;
        assert!(windows_identity_words(&words, false).is_err());
        words[0] = 0x10;
        assert!(windows_identity_words(&words, false).is_err());
        assert!(windows_identity_words(&words, true).is_ok());
    }
}

#[cfg(all(test, unix))]
pub(crate) mod tests {
    use super::*;
    use flate2::write::GzEncoder;
    use flate2::Compression;
    use std::os::unix::fs::PermissionsExt;

    fn private_parent() -> PathBuf {
        let path = std::env::temp_dir().join(random_name("a0-release-payload-test-").unwrap());
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }

    fn tar_header(name: &[u8], size: usize, entry_type: u8) -> [u8; TAR_BLOCK_BYTES] {
        let mut header = [0u8; TAR_BLOCK_BYTES];
        header[..name.len()].copy_from_slice(name);
        write_octal(&mut header[100..108], 0o755);
        write_octal(&mut header[108..116], 0);
        write_octal(&mut header[116..124], 0);
        write_octal(&mut header[124..136], size as u64);
        write_octal(&mut header[136..148], 0);
        header[148..156].fill(b' ');
        header[156] = entry_type;
        header[257..263].copy_from_slice(b"ustar\0");
        header[263..265].copy_from_slice(b"00");
        let checksum = header.iter().map(|byte| *byte as u64).sum();
        write_checksum(&mut header[148..156], checksum);
        header
    }

    fn write_octal(field: &mut [u8], value: u64) {
        field.fill(b'0');
        let text = format!("{value:o}");
        let start = field.len() - 1 - text.len();
        field[start..start + text.len()].copy_from_slice(text.as_bytes());
        field[field.len() - 1] = 0;
    }

    fn write_checksum(field: &mut [u8], value: u64) {
        field.fill(b' ');
        let text = format!("{value:06o}");
        field[..6].copy_from_slice(text.as_bytes());
        field[6] = 0;
    }

    fn tar(entries: &[(&[u8], &[u8], u8)], trailing_zero_blocks: usize) -> Vec<u8> {
        let mut output = Vec::new();
        for (name, bytes, entry_type) in entries {
            output.extend_from_slice(&tar_header(name, bytes.len(), *entry_type));
            output.extend_from_slice(bytes);
            output.resize(
                output.len() + (TAR_BLOCK_BYTES - bytes.len() % TAR_BLOCK_BYTES) % TAR_BLOCK_BYTES,
                0,
            );
        }
        output.resize(output.len() + trailing_zero_blocks * TAR_BLOCK_BYTES, 0);
        output
    }

    fn gzip(bytes: &[u8]) -> Vec<u8> {
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(bytes).unwrap();
        encoder.finish().unwrap()
    }

    fn archive_file(parent: &Path, bytes: &[u8]) -> File {
        let path = parent.join(random_name("archive-").unwrap());
        fs::write(&path, bytes).unwrap();
        File::open(path).unwrap()
    }

    fn proof(bytes: &[u8]) -> VerifiedArtifactBytes {
        VerifiedArtifactBytes::payload_fixture(
            "a0-browser-bridge-linux-x86_64.tar.gz",
            "linux",
            "x86_64",
            bytes,
        )
    }

    pub(crate) fn composition_fixture() -> (PathBuf, VerifiedExecutablePayload, PathBuf) {
        let parent = private_parent();
        let bytes = gzip(&tar(
            &[(b"a0-browser-bridge", b"fixture executable bytes", b'0')],
            2,
        ));
        let archive = archive_file(&parent, &bytes);
        let payload = extract_verified_payload(proof(&bytes), archive, &parent).unwrap();
        let path = payload.staging_file.clone();
        (parent, payload, path)
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn darwin_verification_lease_is_exclusive_and_rechecks_original_bytes() {
        let (parent, mut payload, path) = composition_fixture();
        let handle = payload.verification_handle().unwrap();
        let mut lease = payload.macos_verification_lease().unwrap();
        lease.verify(&handle).unwrap();
        assert!(payload.macos_verification_lease().is_err());
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(&path, b"fixture Executable bytes").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o500)).unwrap();
        assert_eq!(
            lease.verify(&handle),
            Err(ReleasePayloadError::ExecutableHandleChanged)
        );
        drop(lease);
        drop(payload);
        fs::remove_dir_all(parent).unwrap();
    }

    #[test]
    fn exact_signed_archive_derives_one_retained_pathless_executable() {
        let parent = private_parent();
        let executable_bytes = b"fixture executable bytes";
        let archive_bytes = gzip(&tar(&[(b"a0-browser-bridge", executable_bytes, b'0')], 18));
        let archive = archive_file(&parent, &archive_bytes);
        let mut verified =
            extract_verified_payload(proof(&archive_bytes), archive, &parent).unwrap();

        assert_eq!(verified.platform(), "linux");
        assert_eq!(verified.arch(), "x86_64");
        assert_eq!(verified.archive_size(), archive_bytes.len() as u64);
        assert_eq!(verified.executable_size(), executable_bytes.len() as u64);
        assert_eq!(
            verified.executable_sha256(),
            hex(&Sha256::digest(executable_bytes))
        );
        assert!(verified
            .executable
            .as_mut()
            .unwrap()
            .write_all(b"mutation")
            .is_err());
        assert_eq!(
            fs::metadata(&verified.staging_root)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&verified.staging_file)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o500
        );
        verified.verify_retained().unwrap();
        let mut copied = Vec::new();
        verified.copy_executable_to(&mut copied).unwrap();
        assert_eq!(copied, executable_bytes);
        let root = verified.staging_root.clone();
        let file = verified.staging_file.clone();
        drop(verified);
        assert!(!file.exists());
        assert!(!root.exists());
        fs::remove_dir_all(parent).unwrap();
    }

    #[test]
    fn links_devices_traversal_duplicates_and_unexpected_entries_are_rejected() {
        let parent = private_parent();
        let cases = [
            tar(&[(b"a0-browser-bridge", b"x", b'2')], 2),
            tar(&[(b"a0-browser-bridge", b"x", b'1')], 2),
            tar(&[(b"a0-browser-bridge", b"x", b'3')], 2),
            tar(&[(b"../a0-browser-bridge", b"x", b'0')], 2),
            tar(
                &[
                    (b"a0-browser-bridge", b"x", b'0'),
                    (b"a0-browser-bridge", b"x", b'0'),
                ],
                2,
            ),
            tar(
                &[
                    (b"a0-browser-bridge", b"x", b'0'),
                    (b"NOTICE", b"unexpected", b'0'),
                ],
                2,
            ),
        ];
        for bytes in cases {
            let archive_bytes = gzip(&bytes);
            let error = extract_verified_payload(
                proof(&archive_bytes),
                archive_file(&parent, &archive_bytes),
                &parent,
            )
            .unwrap_err();
            assert_eq!(error, ReleasePayloadError::InvalidArchive);
        }

        let zip = b"PK\x03\x04not a tar gzip";
        assert_eq!(
            extract_verified_payload(
                VerifiedArtifactBytes::payload_fixture(
                    "a0-browser-bridge-linux-x86_64.zip",
                    "linux",
                    "x86_64",
                    zip,
                ),
                archive_file(&parent, zip),
                &parent,
            )
            .unwrap_err(),
            ReleasePayloadError::UnsupportedArchive
        );
        fs::remove_dir_all(parent).unwrap();
    }

    #[test]
    fn archive_substitution_members_bombs_and_nonprivate_staging_fail_closed() {
        let parent = private_parent();
        let valid = gzip(&tar(&[(b"a0-browser-bridge", b"trusted", b'0')], 2));
        let changed = gzip(&tar(&[(b"a0-browser-bridge", b"changed", b'0')], 2));
        assert_eq!(
            extract_verified_payload(proof(&valid), archive_file(&parent, &changed), &parent,)
                .unwrap_err(),
            ReleasePayloadError::ArchiveHandleChanged
        );

        let mut concatenated = valid.clone();
        concatenated.extend_from_slice(&gzip(b"second member"));
        assert_eq!(
            extract_verified_payload(
                proof(&concatenated),
                archive_file(&parent, &concatenated),
                &parent,
            )
            .unwrap_err(),
            ReleasePayloadError::InvalidArchive
        );

        let bomb_bytes = vec![b'x'; (MIN_DECOMPRESSED_LIMIT + 1) as usize];
        let bomb = gzip(&tar(&[(b"a0-browser-bridge", &bomb_bytes, b'0')], 2));
        assert_eq!(
            extract_verified_payload(proof(&bomb), archive_file(&parent, &bomb), &parent)
                .unwrap_err(),
            ReleasePayloadError::ArchiveExpansionLimit
        );

        let shared = std::env::temp_dir().join(random_name("a0-release-payload-shared-").unwrap());
        fs::create_dir(&shared).unwrap();
        fs::set_permissions(&shared, fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(
            extract_verified_payload(proof(&valid), archive_file(&parent, &valid), &shared,)
                .unwrap_err(),
            ReleasePayloadError::PrivateStagingUnavailable
        );
        fs::remove_dir(shared).unwrap();
        fs::remove_dir_all(parent).unwrap();
    }
}
