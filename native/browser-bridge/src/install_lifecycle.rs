//! Stable lifecycle effects admitted only by the retained installed-release verifier.
//! No executable bytes, profile credentials, browser profiles, or server records change.
use super::*;
#[path = "credential_cleanup.rs"]
mod credential_cleanup;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Operation {
    Repair,
    Uninstall,
    PrepareCleanup,
}

pub(super) struct Registration {
    pub path: PathBuf,
    pub file: Option<File>,
    pub missing_parent: bool,
}

pub(crate) fn run(
    operation: Operation,
    targets: &[BrowserTarget],
) -> Result<usize, InstallTransactionError> {
    if cfg!(feature = "local-development") || !crate::release::release_trust_configured() {
        return Err(InstallTransactionError::ReleaseEvidenceUnavailable);
    }
    let platform = Platform::current();
    if !matches!(platform, Platform::Macos | Platform::Linux) {
        return Err(InstallTransactionError::WindowsAdapterUnavailable);
    }
    let paths = discover_user_paths().map_err(|_| InstallTransactionError::UserPathsUnavailable)?;
    validate_user_paths(&paths, platform)?;
    inspect_installed_at(
        &paths,
        platform,
        expected_artifact_arch(platform, architecture()),
        crate::release::PRODUCTION_EXTENSION_ORIGINS,
        Some(operation),
        targets,
    )
    .map(|status| status.registration_count)
}

#[cfg(unix)]
fn secure_mode(file: &File, mode: u32) -> Result<(), InstallTransactionError> {
    use std::os::unix::fs::PermissionsExt;
    file.set_permissions(fs::Permissions::from_mode(mode))
        .map_err(|_| InstallTransactionError::Filesystem)?;
    file.sync_all()
        .map_err(|_| InstallTransactionError::Filesystem)
}

#[cfg(not(unix))]
fn secure_mode(_file: &File, _mode: u32) -> Result<(), InstallTransactionError> {
    Err(InstallTransactionError::WindowsAdapterUnavailable)
}

fn recheck(
    path: &Path,
    retained: &File,
    expected: &[u8],
    repair: bool,
) -> Result<(), InstallTransactionError> {
    let mut current = open_owned_for_inspection(path, false, false, repair)?;
    verify_payload_identity(&current, &payload_identity(retained)?)?;
    let (digest, size) = hash_reader(&mut current, MAX_STATE_BYTES as u64)?;
    if size != expected.len() as u64 || digest != sha256(expected) {
        return Err(InstallTransactionError::OwnedRegistrationChanged);
    }
    Ok(())
}

fn publish_missing(path: &Path, bytes: &[u8]) -> Result<(), InstallTransactionError> {
    // Fully write privately, then publish create-only. rename() would overwrite a
    // foreign file created after the ownership check; hard_link() cannot do that.
    let parent = path.parent().ok_or(InstallTransactionError::Filesystem)?;
    let temporary = parent.join(format!(".a0-repair-{}.tmp", random_id()?));
    let mut file = create_new_file(&temporary, 0o600)?;
    let result = (|| {
        file.write_all(bytes)
            .map_err(|_| InstallTransactionError::Filesystem)?;
        file.sync_all()
            .map_err(|_| InstallTransactionError::Filesystem)?;
        fs::hard_link(&temporary, path)
            .map_err(|_| InstallTransactionError::RegistrationConflict)?;
        Ok(())
    })();
    fs::remove_file(&temporary).map_err(|_| InstallTransactionError::Filesystem)?;
    result?;
    sync_dir(parent)
}

#[allow(clippy::too_many_arguments)]
pub(super) fn apply_verified(
    operation: Operation,
    paths: &UserPaths,
    binary: &Path,
    executable: &File,
    state_file: &File,
    state_bytes: &[u8],
    manifest: &str,
    registrations: &[Registration],
) -> Result<(), InstallTransactionError> {
    let repair = operation == Operation::Repair;
    // Complete preflight before the first effect. Recheck again at each effect.
    for registration in registrations {
        if let Some(file) = &registration.file {
            recheck(&registration.path, file, manifest.as_bytes(), repair)?;
        } else if !matches!(fs::symlink_metadata(&registration.path), Err(error) if error.kind() == io::ErrorKind::NotFound)
        {
            return Err(InstallTransactionError::RegistrationConflict);
        }
    }
    let state_path = paths.install_root.join("install-state.json");
    recheck(&state_path, state_file, state_bytes, false)?;
    if operation == Operation::PrepareCleanup {
        return credential_cleanup::prepare(&paths.install_root, state_bytes);
    }
    if repair {
        let repair_effects = || -> Result<(), InstallTransactionError> {
            verify_payload_identity(
                &open_owned_for_inspection(binary, true, true, true)?,
                &payload_identity(executable)?,
            )?;
            // Permission repair is descriptor-bound and follows the signed digest
            // verification; never rewrite or replace executable bytes in place.
            secure_mode(executable, 0o700)?;
            for registration in registrations {
                if let Some(file) = &registration.file {
                    recheck(&registration.path, file, manifest.as_bytes(), true)?;
                    secure_mode(file, 0o600)?;
                } else {
                    if registration.missing_parent {
                        create_registration_parent(&registration.path)?;
                    }
                    publish_missing(&registration.path, manifest.as_bytes())?;
                }
                let file = open_owned_readonly(&registration.path, false, false)?;
                recheck(&registration.path, &file, manifest.as_bytes(), false)?;
            }
            recheck(&state_path, state_file, state_bytes, false)?;
            Ok(())
        };
        return repair_effects().map_err(|_| InstallTransactionError::RepairRecoveryRequired);
    }

    let retire = || -> Result<(), InstallTransactionError> {
        let id = random_id()?;
        let transactions = paths.install_root.join("transactions");
        let work = transactions.join(format!("local-retirement-{id}.d"));
        let marker = transactions.join(format!("local-retirement-{id}.json"));
        ensure_new_private_dir(&work)?;
        let mut snapshot = create_new_file(&work.join("original-install-state.json"), 0o600)?;
        snapshot
            .write_all(state_bytes)
            .map_err(|_| InstallTransactionError::Filesystem)?;
        snapshot
            .sync_all()
            .map_err(|_| InstallTransactionError::Filesystem)?;
        // This intentionally is not an install/update journal. Interrupted local
        // retirement blocks their automatic recovery instead of reinstating access.
        let mut journal = create_new_file(&marker, 0o600)?;
        journal.write_all(b"{\"contract\":\"a0.browser-bridge.local-retirement.v1\",\"credential_cleanup\":\"pending\"}\n")
        .map_err(|_| InstallTransactionError::Filesystem)?;
        journal
            .sync_all()
            .map_err(|_| InstallTransactionError::Filesystem)?;
        sync_dir(&work)?;
        sync_dir(&transactions)?;
        for (index, registration) in registrations.iter().enumerate() {
            if let Some(file) = &registration.file {
                recheck(&registration.path, file, manifest.as_bytes(), false)?;
                let backup = work.join(format!("registration-{index}.json"));
                fs::rename(&registration.path, &backup)
                    .map_err(|_| InstallTransactionError::Filesystem)?;
                recheck(&backup, file, manifest.as_bytes(), false)?;
                sync_dir(
                    registration
                        .path
                        .parent()
                        .ok_or(InstallTransactionError::Filesystem)?,
                )?;
                sync_dir(&work)?;
            }
        }
        recheck(&state_path, state_file, state_bytes, false)?;
        let retired_state = work.join("install-state.json");
        fs::rename(&state_path, &retired_state).map_err(|_| InstallTransactionError::Filesystem)?;
        recheck(&retired_state, state_file, state_bytes, false)?;
        sync_dir(&work)?;
        sync_dir(&paths.install_root)?;
        let backup = paths
            .install_root
            .join(format!("retired-registrations-{id}"));
        fs::rename(&work, &backup).map_err(|_| InstallTransactionError::Filesystem)?;
        sync_dir(&paths.install_root)?;
        fs::remove_file(&marker).map_err(|_| InstallTransactionError::Filesystem)?;
        sync_dir(&transactions)
    };
    retire().map_err(|_| InstallTransactionError::LocalRetirementRecoveryRequired)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    struct Fixture(UserPaths);
    impl Fixture {
        fn new() -> Self {
            let root =
                std::env::temp_dir().join(format!("a0-lifecycle-test-{}", random_id().unwrap()));
            ensure_new_private_dir(&root).unwrap();
            ensure_new_private_dir(&root.join("transactions")).unwrap();
            for (name, bytes) in [
                ("binary", b"unchanged executable".as_slice()),
                ("install-state.json", b"retained signed-state fixture"),
                ("registration.json", b"exact manifest"),
                ("credentials-kept", b"untouched credential fixture"),
            ] {
                let mut file = create_new_file(&root.join(name), 0o600).unwrap();
                file.write_all(bytes).unwrap();
            }
            Self(UserPaths {
                install_root: root,
                home_root: None,
                config_root: None,
            })
        }
        fn apply(
            &self,
            operation: Operation,
            registrations: &[Registration],
        ) -> Result<(), InstallTransactionError> {
            let root = &self.0.install_root;
            apply_verified(
                operation,
                &self.0,
                &root.join("binary"),
                &File::open(root.join("binary")).unwrap(),
                &File::open(root.join("install-state.json")).unwrap(),
                b"retained signed-state fixture",
                "exact manifest",
                registrations,
            )
        }
        fn registration(&self) -> Registration {
            let path = self.0.install_root.join("registration.json");
            Registration {
                file: Some(File::open(&path).unwrap()),
                path,
                missing_parent: false,
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0.install_root);
        }
    }

    #[test]
    fn lifecycle_repair_is_create_only_and_never_rewrites_executable_bytes() {
        let fixture = Fixture::new();
        let root = &fixture.0.install_root;
        let binary = root.join("binary");
        let before = fs::metadata(&binary).unwrap().ino();
        let path = root.join("registration.json");
        fs::remove_file(&path).unwrap();
        fixture
            .apply(
                Operation::Repair,
                &[Registration {
                    path: path.clone(),
                    file: None,
                    missing_parent: false,
                }],
            )
            .unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"exact manifest");
        assert_eq!(fs::read(&binary).unwrap(), b"unchanged executable");
        assert_eq!(fs::metadata(&binary).unwrap().ino(), before);
        assert_eq!(
            fs::metadata(&binary).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(fs::metadata(&path).unwrap().nlink(), 1);
        assert!(publish_missing(&path, b"replacement").is_err());
        assert_eq!(fs::read(&path).unwrap(), b"exact manifest");
    }

    #[test]
    fn lifecycle_foreign_registration_prevents_every_effect() {
        let fixture = Fixture::new();
        let registration = fixture.registration();
        fs::write(&registration.path, b"foreign manifest").unwrap();
        assert_eq!(
            fixture.apply(Operation::Uninstall, &[registration]),
            Err(InstallTransactionError::OwnedRegistrationChanged)
        );
        assert!(fixture.0.install_root.join("install-state.json").exists());
        assert_eq!(
            fs::read_dir(fixture.0.install_root.join("transactions"))
                .unwrap()
                .count(),
            0
        );
    }

    #[test]
    fn lifecycle_local_retirement_preserves_recoverable_bytes_and_credentials() {
        let fixture = Fixture::new();
        let root = &fixture.0.install_root;
        fixture
            .apply(Operation::Uninstall, &[fixture.registration()])
            .unwrap();
        assert!(!root.join("registration.json").exists());
        assert!(!root.join("install-state.json").exists());
        assert_eq!(
            fs::read(root.join("credentials-kept")).unwrap(),
            b"untouched credential fixture"
        );
        assert_eq!(
            fs::read(root.join("binary")).unwrap(),
            b"unchanged executable"
        );
        let backup = fs::read_dir(root)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("retired-registrations-")
            })
            .unwrap();
        assert_eq!(
            fs::read(backup.join("registration-0.json")).unwrap(),
            b"exact manifest"
        );
        assert_eq!(
            fs::read(backup.join("install-state.json")).unwrap(),
            b"retained signed-state fixture"
        );
        assert_eq!(
            fs::metadata(&backup).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(fs::read_dir(root.join("transactions")).unwrap().count(), 0);
    }
}
