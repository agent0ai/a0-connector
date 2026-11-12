//! Windows release staging reuses the exact private artifact ACL implementation.
//! No inherited ACL, reparse, UNC/device/stream, or writable-retention fallback.

use std::ffi::c_void;
use std::fs::File;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};

use super::{FileIdentity, ReleasePayloadError};
use crate::artifact::ArtifactError;

// Keep one implementation of protected current-user/SYSTEM DACL creation,
// current-owner/ACE readback, and retained non-reparse ancestor guards.
#[path = "artifact_windows.rs"]
mod files;
const CREATE_ATTEMPTS: usize = super::CREATE_ATTEMPTS;
fn random_name(prefix: &str) -> Result<String, ArtifactError> {
    super::random_name(prefix).map_err(|_| ArtifactError::PrivateSpoolUnavailable)
}

#[link(name = "kernel32")]
extern "system" {
    fn CreateFileW(
        path: *const u16,
        access: u32,
        share: u32,
        security: *const c_void,
        disposition: u32,
        flags: u32,
        template: *mut c_void,
    ) -> *mut c_void;
    fn GetFileInformationByHandle(handle: *mut c_void, info: *mut [u32; 13]) -> i32;
    fn GetFinalPathNameByHandleW(
        handle: *mut c_void,
        path: *mut u16,
        length: u32,
        flags: u32,
    ) -> u32;
}

fn rejected<T>() -> Result<T, ReleasePayloadError> {
    Err(ReleasePayloadError::PrivateStagingUnavailable)
}

pub(super) fn identity(file: &File, directory: bool) -> Result<FileIdentity, ReleasePayloadError> {
    let mut words = [0u32; 13];
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut words) } == 0 {
        return rejected();
    }
    super::windows_identity_words(&words, directory)
}

pub(super) fn guard_chain(path: &Path) -> Result<Vec<File>, ReleasePayloadError> {
    files::guard_chain(path).map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)
}

pub(super) fn parent(path: &Path) -> Result<PathBuf, ReleasePayloadError> {
    files::parent(Some(path)).map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)
}

pub(super) fn create_root(parent: &Path) -> Result<(PathBuf, FileIdentity), ReleasePayloadError> {
    let path =
        files::create_root(parent).map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    let guards = guard_chain(&path)?;
    let identity = identity(
        guards
            .last()
            .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?,
        true,
    )?;
    Ok((path, identity))
}

pub(super) fn create_file(root: &Path) -> Result<(PathBuf, File), ReleasePayloadError> {
    files::create_file(root, ".exe").map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)
}

pub(super) fn path_identity(path: &Path) -> Result<Option<FileIdentity>, ReleasePayloadError> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return rejected(),
    };
    if metadata.is_dir() {
        let guards = guard_chain(path)?;
        return identity(
            guards
                .last()
                .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?,
            true,
        )
        .map(Some);
    }
    // Only a locally validated, private non-reparse regular file is returned.
    let file = open_readonly(path)?;
    identity(&file, false).map(Some)
}

fn open_readonly(path: &Path) -> Result<File, ReleasePayloadError> {
    open_file(path, true)
}

fn open_file(path: &Path, readonly: bool) -> Result<File, ReleasePayloadError> {
    let _guards = guard_chain(
        path.parent()
            .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?,
    )?;
    let leaf = path
        .file_name()
        .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?
        .encode_wide()
        .collect::<Vec<_>>();
    if leaf.is_empty()
        || leaf.iter().any(|unit| *unit < 32 || *unit == 58)
        || matches!(leaf.last(), Some(32 | 46))
    {
        return rejected();
    }
    let mut wide = path.as_os_str().encode_wide().collect::<Vec<_>>();
    if wide.len() > 4096 || wide.iter().any(|unit| *unit < 32 || *unit == 127) {
        return rejected();
    }
    wide.push(0);
    // GENERIC_READ | READ_CONTROL, FILE_SHARE_READ only, OPEN_EXISTING,
    // FILE_FLAG_OPEN_REPARSE_POINT. No writer/delete sharing survives sealing.
    let access = if readonly { 0x8002_0000 } else { 0x0002_0080 };
    let share = if readonly { 1 } else { 3 };
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            access,
            share,
            null(),
            3,
            0x0020_0000,
            null_mut(),
        )
    };
    if handle == -1isize as *mut c_void {
        return rejected();
    }
    let file = unsafe { File::from_raw_handle(handle) };
    let retained = identity(&file, false)?;
    files::verify_input(&file, path, retained.length)
        .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    Ok(file)
}

pub(super) fn verify_private(path: &Path) -> Result<(), ReleasePayloadError> {
    // Attribute/security-only inspection can coexist with the private writer;
    // the subsequent seal closes that writer before denying all future writes.
    let file = open_file(path, false)?;
    identity(&file, false).map(|_| ())
}

/// Closing the sole writer is necessary before Windows permits a new handle
/// that denies FILE_SHARE_WRITE. Retained ancestors prevent pathname-parent
/// substitution; exact file identity/ACL and later SHA checks reject a changed
/// object during this transition. This is the same-user host trust boundary.
pub(super) fn seal(writer: File, path: &Path) -> Result<File, ReleasePayloadError> {
    let _guards = guard_chain(
        path.parent()
            .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?,
    )?;
    let before = identity(&writer, false)?;
    files::verify_input(&writer, path, before.length)
        .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    drop(writer);
    let readonly = open_readonly(path)?;
    if identity(&readonly, false)? != before {
        return rejected();
    }
    Ok(readonly)
}

/// Reopen the exact original OS-resolved local archive with deny-write/delete
/// sharing. Never interpret an archive entry or caller URL as a local path.
pub(super) fn retain_archive(file: File) -> Result<(File, Vec<File>), ReleasePayloadError> {
    let mut units = vec![0u16; 4097];
    let count = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            units.as_mut_ptr(),
            units.len() as u32,
            0,
        )
    };
    if count == 0 || count as usize >= units.len() {
        return rejected();
    }
    let text = String::from_utf16(&units[..count as usize])
        .map_err(|_| ReleasePayloadError::PrivateStagingUnavailable)?;
    // Windows returns its own normalized DOS path with this prefix. Strip only
    // that OS-produced marker; the shared validator still rejects UNC/devices.
    let text = text
        .strip_prefix(r"\\?\")
        .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?;
    let path = PathBuf::from(text);
    let guards = guard_chain(
        path.parent()
            .ok_or(ReleasePayloadError::PrivateStagingUnavailable)?,
    )?;
    let retained = seal(file, &path)?;
    Ok((retained, guards))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sealed_payload_denies_writers_and_preserves_exact_private_identity() {
        let parent = files::parent(None).unwrap();
        let (root, _) = create_root(&parent).unwrap();
        let (path, mut writer) = create_file(&root).unwrap();
        use std::io::Write;
        writer.write_all(b"test-only release bytes").unwrap();
        writer.sync_all().unwrap();
        let before = identity(&writer, false).unwrap();
        let readonly = seal(writer, &path).unwrap();
        assert_eq!(identity(&readonly, false).unwrap(), before);
        assert!(File::options().write(true).open(&path).is_err());
        assert!(std::fs::remove_file(&path).is_err());
        drop(readonly);
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(root).unwrap();
    }
}
