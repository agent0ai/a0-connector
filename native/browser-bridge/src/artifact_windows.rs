//! Private local-drive Windows artifact files. No inherited-ACL fallback.
//!
//! See Microsoft CreateFileW, GetSecurityInfo and security descriptor string
//! format documentation. Every private object is created with an explicit
//! protected owner/SYSTEM DACL and read back through its retained handle.

use std::ffi::c_void;
use std::fs::File;
use std::mem::size_of;
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle};
use std::path::{Component, Path, PathBuf, Prefix};
use std::ptr::{null, null_mut};

use super::ArtifactError;

type Handle = *mut c_void;
const INVALID_HANDLE: Handle = -1isize as Handle;
const READ_CONTROL: u32 = 0x0002_0000;
const FILE_READ_ATTRIBUTES: u32 = 0x80;
const FILE_ALL_ACCESS: u32 = 0x001f_01ff;
const REPARSE: u32 = 0x400;
const DIRECTORY: u32 = 0x10;
const OPEN_REPARSE: u32 = 0x0020_0000;
const BACKUP_SEMANTICS: u32 = 0x0200_0000;

#[repr(C)]
struct SecurityAttributes {
    length: u32,
    descriptor: *mut c_void,
    inherit: i32,
}
#[repr(C)]
struct Acl {
    revision: u8,
    reserved: u8,
    size: u16,
    count: u16,
    reserved2: u16,
}

#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
    fn CreateDirectoryW(path: *const u16, attributes: *const SecurityAttributes) -> i32;
    fn CreateFileW(
        path: *const u16,
        access: u32,
        share: u32,
        security: *const SecurityAttributes,
        disposition: u32,
        flags: u32,
        template: Handle,
    ) -> Handle;
    fn GetFileInformationByHandle(handle: Handle, info: *mut [u32; 13]) -> i32;
}
#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
    fn GetTokenInformation(
        token: Handle,
        class: u32,
        info: *mut c_void,
        length: u32,
        needed: *mut u32,
    ) -> i32;
    fn ConvertSidToStringSidW(sid: *const c_void, text: *mut *mut u16) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        text: *const u16,
        revision: u32,
        descriptor: *mut *mut c_void,
        size: *mut u32,
    ) -> i32;
    fn GetSecurityInfo(
        handle: Handle,
        kind: u32,
        information: u32,
        owner: *mut *mut c_void,
        group: *mut *mut c_void,
        dacl: *mut *mut Acl,
        sacl: *mut *mut Acl,
        descriptor: *mut *mut c_void,
    ) -> u32;
    fn GetSecurityDescriptorControl(
        descriptor: *const c_void,
        control: *mut u16,
        revision: *mut u32,
    ) -> i32;
    fn GetSecurityDescriptorOwner(
        descriptor: *const c_void,
        owner: *mut *mut c_void,
        defaulted: *mut i32,
    ) -> i32;
    fn GetSecurityDescriptorDacl(
        descriptor: *const c_void,
        present: *mut i32,
        dacl: *mut *mut Acl,
        defaulted: *mut i32,
    ) -> i32;
    fn GetAce(acl: *const Acl, index: u32, ace: *mut *mut c_void) -> i32;
    fn EqualSid(first: *const c_void, second: *const c_void) -> i32;
}

struct LocalAllocation(*mut c_void);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.0);
        }
    }
}
struct Token(Handle);
impl Drop for Token {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

fn denied<T>() -> Result<T, ArtifactError> {
    Err(ArtifactError::PrivateSpoolUnavailable)
}

fn wide(path: &Path) -> Result<Vec<u16>, ArtifactError> {
    let mut value = path.as_os_str().encode_wide().collect::<Vec<_>>();
    if value.is_empty() || value.len() > 4096 || value.iter().any(|unit| *unit < 32 || *unit == 127)
    {
        return denied();
    }
    value.push(0);
    Ok(value)
}

/// Refuse UNC, device namespaces, stream names and normalization aliases.
fn local_components(path: &Path) -> Result<Vec<PathBuf>, ArtifactError> {
    let raw = path.as_os_str().encode_wide().collect::<Vec<_>>();
    if raw
        .split(|unit| *unit == 92 || *unit == 47)
        .any(|part| part == [46] || part == [46, 46])
    {
        return denied();
    }
    let mut parts = path.components();
    let Some(Component::Prefix(prefix)) = parts.next() else {
        return denied();
    };
    if !matches!(prefix.kind(), Prefix::Disk(_)) || parts.next() != Some(Component::RootDir) {
        return denied();
    }
    let mut current = PathBuf::from(prefix.as_os_str());
    current.push("\\");
    let mut paths = vec![current.clone()];
    for component in parts {
        let Component::Normal(name) = component else {
            return denied();
        };
        let units = name.encode_wide().collect::<Vec<_>>();
        if units.is_empty()
            || units.iter().any(|unit| *unit < 32 || *unit == 58)
            || matches!(units.last(), Some(32 | 46))
        {
            return denied();
        }
        current.push(name);
        paths.push(current.clone());
        if paths.len() > 64 {
            return denied();
        }
    }
    if paths.len() < 2 {
        return denied();
    }
    Ok(paths)
}

fn handle_info(file: &File) -> Result<[u32; 13], ArtifactError> {
    let mut info = [0u32; 13];
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0
        || info[0] & REPARSE != 0
    {
        return denied();
    }
    Ok(info)
}

fn open_existing(path: &Path, directory: bool) -> Result<File, ArtifactError> {
    let name = wide(path)?;
    let handle = unsafe {
        CreateFileW(
            name.as_ptr(),
            READ_CONTROL | FILE_READ_ATTRIBUTES,
            if directory { 1 } else { 3 },
            null(),
            3,
            OPEN_REPARSE | if directory { BACKUP_SEMANTICS } else { 0 },
            null_mut(),
        )
    };
    if handle == INVALID_HANDLE {
        return denied();
    }
    let file = unsafe { File::from_raw_handle(handle) };
    let info = handle_info(&file)?;
    if (info[0] & DIRECTORY != 0) != directory {
        return denied();
    }
    Ok(file)
}

/// Keeping these handles open without FILE_SHARE_DELETE prevents replacement
/// of any traversed ancestor while a private path is used by Chrome.
pub(super) fn guard_chain(path: &Path) -> Result<Vec<File>, ArtifactError> {
    local_components(path)?
        .iter()
        .map(|path| open_existing(path, true))
        .collect()
}

pub(super) fn parent(requested: Option<&Path>) -> Result<PathBuf, ArtifactError> {
    let path = match requested {
        Some(path) => path.to_owned(),
        None => std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .ok_or(ArtifactError::PrivateSpoolUnavailable)?,
    };
    let _guards = guard_chain(&path)?;
    Ok(path)
}

fn private_descriptor() -> Result<LocalAllocation, ArtifactError> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), 8, &mut token) } == 0 {
        return denied();
    }
    let token = Token(token);
    let mut needed = 0;
    unsafe {
        GetTokenInformation(token.0, 1, null_mut(), 0, &mut needed);
    }
    if needed < size_of::<Handle>() as u32 || needed > 4096 {
        return denied();
    }
    // Word alignment is required for TOKEN_USER's embedded SID pointer.
    let mut buffer = vec![0usize; (needed as usize).div_ceil(size_of::<usize>())];
    if unsafe { GetTokenInformation(token.0, 1, buffer.as_mut_ptr().cast(), needed, &mut needed) }
        == 0
    {
        return denied();
    }
    let sid = buffer[0] as *const c_void;
    let mut sid_text = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut sid_text) } == 0 {
        return denied();
    }
    let allocation = LocalAllocation(sid_text.cast());
    let mut length = 0;
    while length <= 184 && unsafe { *sid_text.add(length) } != 0 {
        length += 1;
    }
    if length > 184 {
        return denied();
    }
    let sid_string = String::from_utf16(unsafe { std::slice::from_raw_parts(sid_text, length) })
        .map_err(|_| ArtifactError::PrivateSpoolUnavailable)?;
    drop(allocation);
    if !sid_string.starts_with("S-1-")
        || !sid_string
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'S' || byte == b'-')
    {
        return denied();
    }
    let sddl = format!("O:{sid_string}D:P(A;;FA;;;SY)(A;;FA;;;{sid_string})\0")
        .encode_utf16()
        .collect::<Vec<_>>();
    let mut descriptor = null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut descriptor,
            null_mut(),
        )
    } == 0
    {
        return denied();
    }
    Ok(LocalAllocation(descriptor))
}

fn ace_sid(acl: *const Acl, index: u32) -> Result<*const c_void, ArtifactError> {
    let mut ace = null_mut();
    if unsafe { GetAce(acl, index, &mut ace) } == 0 || ace.is_null() {
        return denied();
    }
    let header = ace.cast::<u8>();
    // ACCESS_ALLOWED_ACE with full file rights only; no inherited/object ACEs.
    if unsafe { *header } != 0
        || unsafe { *header.add(1) } != 0
        || unsafe { std::ptr::read_unaligned(header.add(2).cast::<u16>()) } < 20
        || unsafe { std::ptr::read_unaligned(header.add(4).cast::<u32>()) } != FILE_ALL_ACCESS
    {
        return denied();
    }
    Ok(unsafe { header.add(8).cast() })
}

fn verify_private(file: &File, expected: &LocalAllocation) -> Result<(), ArtifactError> {
    let (mut owner, mut acl, mut descriptor) = (null_mut(), null_mut(), null_mut());
    if unsafe {
        GetSecurityInfo(
            file.as_raw_handle(),
            1,
            5,
            &mut owner,
            null_mut(),
            &mut acl,
            null_mut(),
            &mut descriptor,
        )
    } != 0
    {
        return denied();
    }
    let actual = LocalAllocation(descriptor);
    let (mut control, mut revision) = (0u16, 0u32);
    if unsafe { GetSecurityDescriptorControl(actual.0, &mut control, &mut revision) } == 0
        || control & 0x1004 != 0x1004
        || acl.is_null()
        || owner.is_null()
        || unsafe { (*acl).count } != 2
    {
        return denied();
    }
    let (mut expected_owner, mut expected_acl, mut present, mut defaulted) =
        (null_mut(), null_mut(), 0, 0);
    if unsafe { GetSecurityDescriptorOwner(expected.0, &mut expected_owner, &mut defaulted) } == 0
        || unsafe {
            GetSecurityDescriptorDacl(expected.0, &mut present, &mut expected_acl, &mut defaulted)
        } == 0
        || present == 0
        || expected_acl.is_null()
        || expected_owner.is_null()
        || unsafe { EqualSid(owner, expected_owner) } == 0
    {
        return denied();
    }
    let actual_sids = [ace_sid(acl, 0)?, ace_sid(acl, 1)?];
    let expected_sids = [ace_sid(expected_acl, 0)?, ace_sid(expected_acl, 1)?];
    for (index, sid) in actual_sids.iter().enumerate() {
        if unsafe { EqualSid(*sid, expected_sids[index]) } == 0 {
            return denied();
        }
    }
    Ok(())
}

pub(super) fn create_root(parent: &Path) -> Result<PathBuf, ArtifactError> {
    let _parents = guard_chain(parent)?;
    let descriptor = private_descriptor()?;
    let attributes = SecurityAttributes {
        length: size_of::<SecurityAttributes>() as u32,
        descriptor: descriptor.0,
        inherit: 0,
    };
    for _ in 0..super::CREATE_ATTEMPTS {
        let path = parent.join(super::random_name(".a0-browser-artifacts-")?);
        if unsafe { CreateDirectoryW(wide(&path)?.as_ptr(), &attributes) } == 0 {
            continue;
        }
        let result = open_existing(&path, true).and_then(|file| verify_private(&file, &descriptor));
        if let Err(error) = result {
            let _ = std::fs::remove_dir(&path);
            return Err(error);
        }
        return Ok(path);
    }
    denied()
}

pub(super) fn create_file(root: &Path, suffix: &str) -> Result<(PathBuf, File), ArtifactError> {
    let guards = guard_chain(root)?;
    let descriptor = private_descriptor()?;
    verify_private(
        guards
            .last()
            .ok_or(ArtifactError::PrivateSpoolUnavailable)?,
        &descriptor,
    )?;
    let attributes = SecurityAttributes {
        length: size_of::<SecurityAttributes>() as u32,
        descriptor: descriptor.0,
        inherit: 0,
    };
    for _ in 0..super::CREATE_ATTEMPTS {
        let path = root.join(format!("{}{suffix}", super::random_name("attachment-")?));
        let handle = unsafe {
            CreateFileW(
                wide(&path)?.as_ptr(),
                0xc000_0000 | READ_CONTROL,
                1,
                &attributes,
                1,
                OPEN_REPARSE | 0x80,
                null_mut(),
            )
        };
        if handle == INVALID_HANDLE {
            continue;
        }
        let file = unsafe { File::from_raw_handle(handle) };
        let result = verify_private(&file, &descriptor).and_then(|_| handle_info(&file));
        match result {
            Ok(info) if info[0] & DIRECTORY == 0 && info[10] == 1 => return Ok((path, file)),
            _ => {
                drop(file);
                let _ = std::fs::remove_file(&path);
                return denied();
            }
        }
    }
    denied()
}

pub(super) fn verify_input(file: &File, path: &Path, byte_count: u64) -> Result<(), ArtifactError> {
    let _parents = guard_chain(
        path.parent()
            .ok_or(ArtifactError::PrivateSpoolUnavailable)?,
    )?;
    let named = open_existing(path, false)?;
    let expected = private_descriptor()?;
    verify_private(file, &expected)?;
    verify_private(&named, &expected)?;
    let retained = handle_info(file)?;
    let current = handle_info(&named)?;
    if retained[7] != current[7]
        || retained[11..13] != current[11..13]
        || retained[10] != 1
        || current[10] != 1
        || retained[0] & DIRECTORY != 0
        || ((retained[8] as u64) << 32 | retained[9] as u64) != byte_count
    {
        return denied();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn refuses_remote_device_stream_and_alias_paths() {
        for path in [
            r"\\server\share\file",
            r"\\?\C:\file",
            r"C:\data\file:stream",
            r"C:\data\trailing.",
            r"C:\data\..\other",
        ] {
            assert!(local_components(Path::new(path)).is_err());
        }
    }
    #[test]
    fn creates_only_verified_private_local_files_and_denies_external_writers() {
        let parent = parent(None).unwrap();
        let root = create_root(&parent).unwrap();
        let (path, file) = create_file(&root, ".txt").unwrap();
        verify_input(&file, &path, 0).unwrap();
        assert!(File::options().write(true).open(&path).is_err());
        drop(file);
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(root).unwrap();
    }
}
