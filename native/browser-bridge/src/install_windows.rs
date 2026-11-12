//! Windows filesystem operations used by the shared installation transaction.
use super::*;
use crate::artifact::ArtifactError;
use std::os::windows::{ffi::OsStrExt, io::AsRawHandle};
use windows_sys::Win32::{
    Foundation::{GetLastError, ERROR_LOCK_VIOLATION},
    Storage::FileSystem::{
        LockFileEx, MoveFileExW, UnlockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    },
    System::IO::OVERLAPPED,
};

#[path = "artifact_windows.rs"]
pub(super) mod files;
#[path = "registry_windows.rs"]
pub(super) mod registry;
const CREATE_ATTEMPTS: usize = 32;
fn random_name(prefix: &str) -> Result<String, ArtifactError> {
    super::random_id()
        .map(|id| format!("{prefix}{id}"))
        .map_err(|_| ArtifactError::PrivateSpoolUnavailable)
}
fn denied(_: ArtifactError) -> InstallTransactionError {
    InstallTransactionError::PrivateInstallRootUnavailable
}
pub(super) fn validate_path(path: &Path) -> Result<(), InstallTransactionError> {
    files::local_components(path).map(|_| ()).map_err(denied)
}

pub(super) fn verify_directory(path: &Path) -> Result<(), InstallTransactionError> {
    files::verify_private_directory(path)
        .map(|_| ())
        .map_err(denied)
}
pub(super) fn create_directory(path: &Path) -> Result<(), InstallTransactionError> {
    files::create_private_directory(path).map_err(denied)
}
pub(super) fn ensure_root(path: &Path) -> Result<(), InstallTransactionError> {
    files::local_components(path).map_err(denied)?;
    let mut missing = Vec::new();
    let mut parent = path;
    loop {
        match fs::symlink_metadata(parent) {
            Ok(_) => {
                files::guard_chain(parent).map_err(denied)?;
                break;
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                missing.push(parent);
                parent = parent
                    .parent()
                    .ok_or(InstallTransactionError::UserPathsUnavailable)?;
            }
            Err(_) => return Err(InstallTransactionError::PrivateInstallRootUnavailable),
        }
    }
    for path in missing.into_iter().rev() {
        create_directory(path)?;
    }
    verify_directory(path)
}
pub(super) fn create_file(path: &Path) -> Result<File, InstallTransactionError> {
    files::create_private_file(path).map_err(denied)
}
pub(super) fn open_file(path: &Path) -> Result<File, InstallTransactionError> {
    files::open_private_file(path).map_err(|_| InstallTransactionError::OwnedStateInvalid)
}
pub(super) fn open_lifecycle(path: &Path) -> Result<File, InstallTransactionError> {
    // DELETE belongs only to this retained handle; competing deletes stay denied.
    files::open_private_file_with_access(path, 0x8003_0000, 1)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)
}
pub(super) fn inspect_file(path: &Path) -> Result<File, InstallTransactionError> {
    files::open_private_file_with_access(path, 0x8002_0000, 5)
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)
}
pub(super) fn secure_handle(file: &File) -> Result<(), InstallTransactionError> {
    files::secure_private_handle(file).map_err(denied)
}
pub(super) fn repair_file(path: &Path, retained: bool) -> Result<File, InstallTransactionError> {
    files::open_repair_file(path, retained).map_err(denied)
}
pub(super) fn retire(file: &File, destination: &Path) -> Result<(), InstallTransactionError> {
    use windows_sys::Win32::Storage::FileSystem::{
        FileRenameInfo, SetFileInformationByHandle, FILE_RENAME_INFO,
    };
    files::local_components(destination).map_err(denied)?;
    let _parents = files::verify_private_directory(
        destination
            .parent()
            .ok_or(InstallTransactionError::Filesystem)?,
    )
    .map_err(denied)?;
    let name: Vec<u16> = destination.as_os_str().encode_wide().collect();
    let offset = std::mem::offset_of!(FILE_RENAME_INFO, FileName);
    let length = offset + name.len() * 2;
    let mut buffer = vec![0usize; length.div_ceil(std::mem::size_of::<usize>())];
    let info = buffer.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    unsafe {
        (*info).FileNameLength = (name.len() * 2) as u32;
        std::ptr::copy_nonoverlapping(name.as_ptr(), (*info).FileName.as_mut_ptr(), name.len());
        if SetFileInformationByHandle(
            file.as_raw_handle(),
            FileRenameInfo,
            info.cast(),
            length as u32,
        ) == 0
        {
            return Err(InstallTransactionError::Filesystem);
        }
    }
    files::verify_input(file, destination, payload_identity(file)?.length).map_err(denied)
}
pub(super) fn verify_mode(path: &Path) -> Result<(), InstallTransactionError> {
    if fs::symlink_metadata(path)
        .map_err(|_| InstallTransactionError::Filesystem)?
        .is_dir()
    {
        verify_directory(path)
    } else {
        open_file(path).map(|_| ())
    }
}
pub(super) fn lock(file: &File) -> Result<bool, InstallTransactionError> {
    let mut overlap = OVERLAPPED::default();
    if unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlap,
        )
    } != 0
    {
        return Ok(true);
    }
    if unsafe { GetLastError() } == ERROR_LOCK_VIOLATION {
        Ok(false)
    } else {
        Err(InstallTransactionError::Filesystem)
    }
}
pub(super) fn unlock(file: &File) -> Result<(), InstallTransactionError> {
    if unsafe { UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut OVERLAPPED::default()) } != 0 {
        Ok(())
    } else {
        Err(InstallTransactionError::Filesystem)
    }
}
pub(super) fn rename(from: &Path, to: &Path, replace: bool) -> Result<(), InstallTransactionError> {
    files::local_components(from).map_err(denied)?;
    files::local_components(to).map_err(denied)?;
    let source: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
    let destination: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
    let flags = MOVEFILE_WRITE_THROUGH
        | if replace {
            MOVEFILE_REPLACE_EXISTING
        } else {
            0
        };
    if unsafe { MoveFileExW(source.as_ptr(), destination.as_ptr(), flags) } == 0 {
        Err(InstallTransactionError::Filesystem)
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn private_install_files_reject_inherited_acls_links_and_competing_writers() {
        let root = std::env::temp_dir().join(format!(
            "a0-windows-install-{}",
            super::super::random_id().unwrap()
        ));
        create_directory(&root).unwrap();
        verify_directory(&root).unwrap();
        let path = root.join("state.json");
        let mut writer = create_file(&path).unwrap();
        writer.write_all(b"private test state").unwrap();
        writer.sync_all().unwrap();
        assert!(create_file(&path).is_err());
        drop(writer);
        let retained = open_file(&path).unwrap();
        assert!(File::options().write(true).open(&path).is_err());
        assert!(fs::remove_file(&path).is_err());
        assert!(lock(&retained).unwrap());
        let other = open_file(&path).unwrap();
        assert!(!lock(&other).unwrap());
        unlock(&retained).unwrap();
        assert!(lock(&other).unwrap());
        unlock(&other).unwrap();
        drop((retained, other));
        let link = root.join("linked.json");
        fs::hard_link(&path, &link).unwrap();
        assert!(open_file(&path).is_err());
        fs::remove_file(link).unwrap();
        let foreign = root.join("inherited.json");
        fs::write(&foreign, b"untrusted ACL").unwrap();
        assert!(open_file(&foreign).is_err());
        fs::remove_file(foreign).unwrap();
        fs::remove_file(path).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn retained_retirement_moves_only_the_verified_file() {
        let root = std::env::temp_dir().join(format!("a0-retire-test-{}", random_id().unwrap()));
        create_directory(&root).unwrap();
        let path = root.join("state.json");
        let mut writer = create_file(&path).unwrap();
        writer.write_all(b"owned").unwrap();
        drop(writer);
        let file = open_lifecycle(&path).unwrap();
        assert!(fs::remove_file(&path).is_err());
        let inspected = inspect_file(&path).unwrap();
        verify_payload_identity(&inspected, &payload_identity(&file).unwrap()).unwrap();
        drop(inspected);
        let retired = root.join("retired.json");
        retire(&file, &retired).unwrap();
        assert!(!path.exists());
        assert!(fs::remove_file(&retired).is_err());
        drop(file);
        fs::remove_file(retired).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn windows_repair_and_retirement_keep_handle_ownership() {
        let root = std::env::temp_dir().join(format!("a0-lifecycle-test-{}", random_id().unwrap()));
        create_directory(&root).unwrap();
        create_directory(&root.join("transactions")).unwrap();
        create_directory(&root.join("manifests")).unwrap();
        create_directory(&root.join("manifests/chrome")).unwrap();
        let paths = UserPaths {
            install_root: root.clone(),
            home_root: None,
            config_root: None,
        };
        let binary = root.join("a0-browser-bridge.exe");
        fs::write(&binary, b"signed digest fixture checked by caller").unwrap();
        assert!(open_file(&binary).is_err());
        let executable = repair_file(&binary, true).unwrap();
        let state_path = root.join("install-state.json");
        let state_bytes = b"owned test state";
        let mut writer = create_file(&state_path).unwrap();
        writer.write_all(state_bytes).unwrap();
        drop(writer);
        let state = open_lifecycle(&state_path).unwrap();
        let manifest_path = root
            .join("manifests/chrome")
            .join(format!("{NATIVE_HOST_NAME}.json"));
        let manifest = "owned test manifest";
        let registrations = [lifecycle::Registration {
            path: manifest_path.clone(),
            file: None,
            missing_parent: false,
        }];
        lifecycle::apply_verified(
            lifecycle::Operation::Repair,
            &paths,
            &binary,
            &executable,
            &state,
            state_bytes,
            manifest,
            &registrations,
        )
        .unwrap();
        assert!(files::verify_private_handle(&executable).is_ok());
        assert!(registry::present(&manifest_path, true).unwrap());
        let registrations = [lifecycle::Registration {
            path: manifest_path.clone(),
            file: Some(open_lifecycle(&manifest_path).unwrap()),
            missing_parent: false,
        }];
        lifecycle::apply_verified(
            lifecycle::Operation::Uninstall,
            &paths,
            &binary,
            &executable,
            &state,
            state_bytes,
            manifest,
            &registrations,
        )
        .unwrap();
        assert!(!state_path.exists());
        assert!(!manifest_path.exists());
        assert!(!registry::present(&manifest_path, true).unwrap());
        assert!(binary.exists());
        assert!(fs::read_dir(root.join("transactions"))
            .unwrap()
            .next()
            .is_none());
        drop((registrations, state, executable));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn windows_transaction_reuses_and_recovers_with_registry_double() {
        let root =
            std::env::temp_dir().join(format!("a0-transaction-test-{}", random_id().unwrap()));
        create_directory(&root).unwrap();
        let paths = UserPaths {
            install_root: root.join("install"),
            home_root: None,
            config_root: None,
        };
        let install = |version: &str, fault: Fault| {
            let path = root.join(format!("candidate-{}", random_id().unwrap()));
            let mut writer = create_file(&path).unwrap();
            writer.write_all(version.as_bytes()).unwrap();
            drop(writer);
            let candidate = FullyVerifiedInstallCandidate::test_fixture(
                open_file(&path).unwrap(),
                version,
                Platform::Windows,
                "x86_64",
            )
            .unwrap();
            let result = execute_at(
                InstallOperation::Install,
                vec![
                    BrowserTarget::Explicit(BrowserId::Chrome),
                    BrowserTarget::Explicit(BrowserId::Brave),
                ],
                InstallSource::A0Cli,
                candidate,
                Platform::Windows,
                "x86_64",
                paths.clone(),
                1000,
                fault,
            );
            fs::remove_file(path).unwrap();
            result
        };
        let first = install("2.12.0", Fault::None).unwrap();
        assert_eq!(first.registration_count, 1);
        assert!(install("2.12.0", Fault::None).unwrap().already_current);
        let state = fs::read(paths.install_root.join("install-state.json")).unwrap();
        assert_eq!(
            install("2.12.1", Fault::FailAfterFirstRegistration).unwrap_err(),
            InstallTransactionError::InjectedFailure
        );
        assert_eq!(
            fs::read(paths.install_root.join("install-state.json")).unwrap(),
            state
        );
        assert_eq!(
            install("2.12.1", Fault::CrashAfterFirstRegistration).unwrap_err(),
            InstallTransactionError::InjectedCrash
        );
        assert!(!install("2.12.1", Fault::None).unwrap().already_current);
        assert_eq!(
            install("2.12.2", Fault::CrashAfterStateCommit).unwrap_err(),
            InstallTransactionError::InjectedCrash
        );
        assert!(install("2.12.2", Fault::None).unwrap().already_current);
        assert!(fs::read_dir(paths.install_root.join("transactions"))
            .unwrap()
            .next()
            .is_none());
        fs::remove_dir_all(root).unwrap();
    }
}
