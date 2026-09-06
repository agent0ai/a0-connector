//! Explicit source-built local-development installation.
//!
//! This channel is deliberately separate from stable release verification. A
//! local-development binary may install only itself, for one compiled extension
//! origin, under a separate host name/root/state/key namespace. It never turns
//! local bytes into signed-release evidence or runtime admission.

use std::io::{self, Write};

use crate::json;
use crate::registry::BrowserId;
use crate::{
    COMPANION_VERSION, DEVELOPMENT_CHANNEL, DEVELOPMENT_EXTENSION_ID, DEVELOPMENT_NATIVE_HOST_NAME,
    DEVELOPMENT_RESULT_CONTRACT, EXIT_USAGE,
};

const STATE_INSTALLED: &str = "installed";
const STATE_NOT_INSTALLED: &str = "not_installed";
const STATE_UNHEALTHY: &str = "unhealthy";
#[cfg(all(feature = "local-development", not(unix)))]
const STATE_UNSUPPORTED: &str = "unsupported";
const STATE_BLOCKED: &str = "blocked";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DevelopmentAction {
    Install,
    Update,
    Status,
    Uninstall,
}

impl DevelopmentAction {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Install => "install",
            Self::Update => "update",
            Self::Status => "status",
            Self::Uninstall => "uninstall",
        }
    }
}

struct Options {
    action: DevelopmentAction,
    browsers: Vec<BrowserId>,
    automatic: bool,
    confirmed: bool,
    json: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct DevelopmentResult {
    action: DevelopmentAction,
    state: &'static str,
    reason_code: &'static str,
    registered_browsers: Vec<BrowserId>,
    already_current: bool,
    mutation_allowed: bool,
    exit_code: u8,
}

impl DevelopmentResult {
    fn blocked(action: DevelopmentAction, reason_code: &'static str, exit_code: u8) -> Self {
        Self {
            action,
            state: STATE_BLOCKED,
            reason_code,
            registered_browsers: Vec::new(),
            already_current: false,
            mutation_allowed: false,
            exit_code,
        }
    }

    fn to_json(&self) -> String {
        json::object(&[
            ("contract", json::quote(DEVELOPMENT_RESULT_CONTRACT)),
            ("schema_version", "1".to_owned()),
            ("channel", json::quote(DEVELOPMENT_CHANNEL)),
            ("action", json::quote(self.action.as_str())),
            ("state", json::quote(self.state)),
            ("reason_code", json::quote(self.reason_code)),
            ("companion_version", json::quote(COMPANION_VERSION)),
            (
                "native_host_name",
                json::quote(DEVELOPMENT_NATIVE_HOST_NAME),
            ),
            ("extension_id", json::quote(DEVELOPMENT_EXTENSION_ID)),
            (
                "registered_browsers",
                json::string_array(
                    self.registered_browsers
                        .iter()
                        .map(|browser| browser.as_str()),
                ),
            ),
            (
                "registration_count",
                self.registered_browsers.len().to_string(),
            ),
            ("already_current", self.already_current.to_string()),
            ("mutation_allowed", self.mutation_allowed.to_string()),
            ("exit_code", self.exit_code.to_string()),
        ])
    }
}

pub fn run(args: &[String]) -> u8 {
    let json_requested = args.iter().any(|argument| argument == "--json");
    let parsed = parse_options(args);
    let (result, json_mode) = match parsed {
        Ok(options) => {
            let json_mode = options.json;
            let result = if matches!(
                options.action,
                DevelopmentAction::Install
                    | DevelopmentAction::Update
                    | DevelopmentAction::Uninstall
            ) && !options.confirmed
            {
                DevelopmentResult::blocked(
                    options.action,
                    "DEVELOPMENT_CONFIRMATION_REQUIRED",
                    EXIT_USAGE,
                )
            } else {
                dispatch(options)
            };
            (result, json_mode)
        }
        Err((action, reason_code)) => (
            DevelopmentResult::blocked(action, reason_code, EXIT_USAGE),
            json_requested,
        ),
    };
    write_result(&result, json_mode);
    result.exit_code
}

fn parse_options(args: &[String]) -> Result<Options, (DevelopmentAction, &'static str)> {
    let action = match args.first().map(String::as_str) {
        Some("install") => DevelopmentAction::Install,
        Some("update") => DevelopmentAction::Update,
        Some("status") => DevelopmentAction::Status,
        Some("uninstall") => DevelopmentAction::Uninstall,
        _ => return Err((DevelopmentAction::Status, "DEVELOPMENT_ACTION_INVALID")),
    };
    let mut browsers = Vec::new();
    let mut automatic = false;
    let mut confirmed = false;
    let mut json = false;
    let mut index = 1;
    while index < args.len() {
        match args[index].as_str() {
            "--json" if !json => {
                json = true;
                index += 1;
            }
            "--yes"
                if !confirmed
                    && matches!(
                        action,
                        DevelopmentAction::Install
                            | DevelopmentAction::Update
                            | DevelopmentAction::Uninstall
                    ) =>
            {
                confirmed = true;
                index += 1;
            }
            "--browser" if action == DevelopmentAction::Install => {
                let Some(value) = args.get(index + 1) else {
                    return Err((action, "BROWSER_VALUE_REQUIRED"));
                };
                if value == "auto" {
                    automatic = true;
                } else {
                    let Some(browser) = BrowserId::parse(value) else {
                        return Err((action, "UNSUPPORTED_BROWSER"));
                    };
                    if !browsers.contains(&browser) {
                        browsers.push(browser);
                    }
                }
                index += 2;
            }
            _ => return Err((action, "DEVELOPMENT_ARGUMENT_UNEXPECTED")),
        }
    }
    if action == DevelopmentAction::Install {
        if automatic && !browsers.is_empty() {
            return Err((action, "AUTO_BROWSER_MUST_BE_EXCLUSIVE"));
        }
        if browsers.is_empty() && !automatic {
            automatic = true;
        }
        browsers.sort();
    }
    Ok(Options {
        action,
        browsers,
        automatic,
        confirmed,
        json,
    })
}

#[cfg(not(feature = "local-development"))]
fn dispatch(options: Options) -> DevelopmentResult {
    DevelopmentResult::blocked(
        options.action,
        "DEVELOPMENT_BUILD_REQUIRED",
        crate::EXIT_RELEASE_UNAVAILABLE,
    )
}

#[cfg(all(feature = "local-development", not(unix)))]
fn dispatch(options: Options) -> DevelopmentResult {
    DevelopmentResult {
        action: options.action,
        state: STATE_UNSUPPORTED,
        reason_code: "WINDOWS_DEVELOPMENT_INSTALL_UNAVAILABLE",
        registered_browsers: Vec::new(),
        already_current: false,
        mutation_allowed: false,
        exit_code: crate::EXIT_RELEASE_UNAVAILABLE,
    }
}

#[cfg(all(feature = "local-development", unix))]
fn dispatch(options: Options) -> DevelopmentResult {
    unix::dispatch(options)
}

fn write_result(result: &DevelopmentResult, json_mode: bool) {
    let mut output = io::stdout().lock();
    if json_mode {
        let _ = writeln!(output, "{}", result.to_json());
    } else {
        let _ = writeln!(
            output,
            "Browser Bridge local development {}: {} ({})",
            result.action.as_str(),
            result.state,
            result.reason_code,
        );
    }
}

#[cfg(all(feature = "local-development", unix))]
mod unix {
    use std::collections::{BTreeMap, BTreeSet};
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, Read, Seek, SeekFrom, Write};
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
    use std::path::{Path, PathBuf};
    use std::thread;
    use std::time::Duration;

    use sha2::{Digest, Sha256};

    use super::{
        DevelopmentAction, DevelopmentResult, Options, STATE_INSTALLED, STATE_NOT_INSTALLED,
        STATE_UNHEALTHY,
    };
    use crate::json::{self, Value};
    use crate::manifest::generate_development_manifest;
    use crate::platform::{architecture, discover_user_paths, Platform, UserPaths};
    use crate::registry::{
        discover_stable_browsers, registration_location, BrowserId, RegistrationLocation, BROWSERS,
    };
    use crate::{
        COMPANION_VERSION, DEVELOPMENT_CHANNEL, DEVELOPMENT_EXTENSION_ORIGIN,
        DEVELOPMENT_TRUST_CONTRACT, EXIT_INTEGRITY_OR_POLICY, EXIT_NOT_INSTALLED, EXIT_OK,
        EXIT_PARTIAL, NATIVE_HOST_NAME,
    };

    const STATE_FILE: &str = "development-install-state.json";
    const STATE_CONTRACT: &str = "a0.browser-bridge.development-state.v1";
    const UPDATE_JOURNAL_FILE: &str = "development-update-journal.json";
    const UPDATE_JOURNAL_CONTRACT: &str = "a0.browser-bridge.development-update-journal.v1";
    const MAX_UPDATE_JOURNAL_BYTES: usize = 64 * 1024;
    const MAX_BINARY_BYTES: u64 = 512 * 1024 * 1024;
    const MAX_STATE_BYTES: usize = 64 * 1024;
    const MAX_MANIFEST_BYTES: usize = 64 * 1024;
    const LOCK_ATTEMPTS: usize = 40;
    const LOCK_WAIT: Duration = Duration::from_millis(25);
    const LOCK_MAGIC: &[u8] = b"a0-browser-bridge-development-install-lock-v1\n";

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum DevelopmentError {
        SourceInvalid,
        UserPathsUnavailable,
        UnsupportedPlatform,
        BrowserDiscoveryUnavailable,
        NoBrowserTargets,
        RegistrationUnavailable,
        RegistrationConflict,
        StateInvalid,
        InstallUnhealthy,
        InstallBusy,
        ReinstallRequired,
        UpdateNotInstalled,
        UpdateRecoveryRequired,
        PartialMutation,
        Filesystem,
        EntropyUnavailable,
    }

    impl DevelopmentError {
        const fn reason_code(self) -> &'static str {
            match self {
                Self::SourceInvalid => "DEVELOPMENT_SOURCE_BUILD_INVALID",
                Self::UserPathsUnavailable => "USER_ROOT_UNAVAILABLE",
                Self::UnsupportedPlatform => "UNSUPPORTED_PLATFORM",
                Self::BrowserDiscoveryUnavailable => "BROWSER_DISCOVERY_NOT_AVAILABLE",
                Self::NoBrowserTargets => "NO_SUPPORTED_BROWSER_FOUND",
                Self::RegistrationUnavailable => "BROWSER_REGISTRATION_UNAVAILABLE",
                Self::RegistrationConflict => "REGISTRATION_CONFLICT",
                Self::StateInvalid => "DEVELOPMENT_STATE_INVALID",
                Self::InstallUnhealthy => "DEVELOPMENT_INSTALL_UNHEALTHY",
                Self::InstallBusy => "DEVELOPMENT_INSTALL_BUSY",
                Self::ReinstallRequired => "DEVELOPMENT_REINSTALL_REQUIRED",
                Self::UpdateNotInstalled => "DEVELOPMENT_UPDATE_REQUIRES_INSTALL",
                Self::UpdateRecoveryRequired => "DEVELOPMENT_UPDATE_RECOVERY_REQUIRED",
                Self::PartialMutation => "DEVELOPMENT_CLEANUP_PENDING",
                Self::Filesystem => "DEVELOPMENT_FILESYSTEM_ERROR",
                Self::EntropyUnavailable => "DEVELOPMENT_ENTROPY_UNAVAILABLE",
            }
        }
    }

    #[derive(Clone, Debug, Eq, PartialEq)]
    struct FileIdentity {
        device: u64,
        inode: u64,
        size: u64,
    }

    struct SourceBuild {
        file: File,
        identity: FileIdentity,
        sha256: String,
    }

    #[derive(Clone, Debug, Eq, PartialEq)]
    struct DevelopmentState {
        install_id: String,
        companion_version: String,
        platform: Platform,
        architecture: String,
        active_binary_sha256: String,
        active_binary_size: u64,
        registered_browsers: Vec<BrowserId>,
        registration_path_sha256: Vec<String>,
        manifest_sha256: String,
    }

    #[derive(Clone, Debug, Eq, PartialEq)]
    struct DevelopmentUpdateJournal {
        transaction_id: String,
        old_state: DevelopmentState,
        new_state: DevelopmentState,
    }

    struct UpdateTarget {
        path: PathBuf,
        stage: PathBuf,
        old: Vec<u8>,
        new: Vec<u8>,
        max_bytes: usize,
    }

    struct OwnedFileSnapshot {
        device: u64,
        inode: u64,
        links: u64,
        bytes: Vec<u8>,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum UpdateFault {
        None,
        #[cfg(test)]
        AfterJournal,
        #[cfg(test)]
        AfterFirstManifest,
    }

    impl DevelopmentState {
        fn encode(&self) -> String {
            json::object(&[
                ("contract", json::quote(STATE_CONTRACT)),
                ("schema_version", "1".to_owned()),
                ("channel", json::quote(DEVELOPMENT_CHANNEL)),
                ("install_id", json::quote(&self.install_id)),
                ("companion_version", json::quote(&self.companion_version)),
                ("native_host_name", json::quote(NATIVE_HOST_NAME)),
                (
                    "extension_origin",
                    json::quote(DEVELOPMENT_EXTENSION_ORIGIN),
                ),
                ("trust_contract", json::quote(DEVELOPMENT_TRUST_CONTRACT)),
                ("platform", json::quote(self.platform.as_str())),
                ("architecture", json::quote(&self.architecture)),
                (
                    "active_binary_sha256",
                    json::quote(&self.active_binary_sha256),
                ),
                ("active_binary_size", self.active_binary_size.to_string()),
                (
                    "registered_browsers",
                    json::string_array(
                        self.registered_browsers
                            .iter()
                            .map(|browser| browser.as_str()),
                    ),
                ),
                (
                    "registration_path_sha256",
                    json::string_array(self.registration_path_sha256.iter().map(String::as_str)),
                ),
                ("manifest_sha256", json::quote(&self.manifest_sha256)),
                ("source", json::quote("executing-local-source-build")),
            ])
        }

        fn as_value(&self) -> Result<Value, DevelopmentError> {
            json::parse(self.encode().as_bytes()).map_err(|_| DevelopmentError::StateInvalid)
        }
    }

    impl DevelopmentUpdateJournal {
        fn encode(&self) -> Result<String, DevelopmentError> {
            Ok(json::object(&[
                ("contract", json::quote(UPDATE_JOURNAL_CONTRACT)),
                ("schema_version", "1".to_owned()),
                ("transaction_id", json::quote(&self.transaction_id)),
                ("old_state", self.old_state.as_value()?.encode()),
                ("new_state", self.new_state.as_value()?.encode()),
            ]))
        }
    }

    pub(super) fn dispatch(options: Options) -> DevelopmentResult {
        let action = options.action;
        let result = match action {
            DevelopmentAction::Install => install(options),
            DevelopmentAction::Update => update(),
            DevelopmentAction::Status => status(),
            DevelopmentAction::Uninstall => uninstall(),
        };
        match result {
            Ok(result) => result,
            Err(DevelopmentError::PartialMutation) => DevelopmentResult {
                action,
                state: STATE_UNHEALTHY,
                reason_code: DevelopmentError::PartialMutation.reason_code(),
                registered_browsers: Vec::new(),
                already_current: false,
                mutation_allowed: false,
                exit_code: EXIT_PARTIAL,
            },
            Err(error) => DevelopmentResult::blocked(
                action,
                error.reason_code(),
                match error {
                    DevelopmentError::NoBrowserTargets | DevelopmentError::UpdateNotInstalled => {
                        EXIT_NOT_INSTALLED
                    }
                    DevelopmentError::BrowserDiscoveryUnavailable
                    | DevelopmentError::UserPathsUnavailable => crate::EXIT_RELEASE_UNAVAILABLE,
                    _ => EXIT_INTEGRITY_OR_POLICY,
                },
            ),
        }
    }

    fn install(options: Options) -> Result<DevelopmentResult, DevelopmentError> {
        require_supported_build()?;
        let platform = Platform::current();
        let paths = paths_for(platform)?;
        let source = open_executing_source_build()?;
        let browsers = if options.automatic {
            discover_stable_browsers(platform, &paths)
                .map_err(|_| DevelopmentError::BrowserDiscoveryUnavailable)?
        } else {
            options.browsers
        };
        if browsers.is_empty() {
            return Err(DevelopmentError::NoBrowserTargets);
        }
        ensure_private_root(&paths.install_root)?;
        let _lock = InstallLock::acquire(&paths.install_root.join("development-install.lock"))?;
        require_no_update_journal(&paths)?;
        let state_path = paths.install_root.join(STATE_FILE);
        let old_state = load_state(&state_path)?;
        if let Some(state) = old_state.as_ref() {
            verify_owned_state(state, &paths)?;
        }

        let binary = binary_path(
            &paths.install_root,
            COMPANION_VERSION,
            platform,
            architecture(),
            &source.sha256,
        );
        let manifest =
            generate_development_manifest(&binary).map_err(|_| DevelopmentError::SourceInvalid)?;
        let manifest_sha256 = sha256(manifest.as_bytes());
        let registrations = registration_paths(&browsers, platform, &paths)?;
        validate_registration_collisions(old_state.as_ref(), &registrations, &paths)?;
        let registration_digests = registrations
            .keys()
            .map(|path| path_bytes(path).map(|bytes| sha256(&bytes)))
            .collect::<Result<Vec<_>, DevelopmentError>>()?;
        let state = DevelopmentState {
            install_id: old_state
                .as_ref()
                .map(|state| state.install_id.clone())
                .unwrap_or(random_id()?),
            companion_version: COMPANION_VERSION.to_owned(),
            platform,
            architecture: architecture().to_owned(),
            active_binary_sha256: source.sha256.clone(),
            active_binary_size: source.identity.size,
            registered_browsers: browsers.clone(),
            registration_path_sha256: registration_digests,
            manifest_sha256,
        };
        if let Some(old) = old_state.as_ref() {
            if old == &state {
                return Ok(DevelopmentResult {
                    action: DevelopmentAction::Install,
                    state: STATE_INSTALLED,
                    reason_code: "DEVELOPMENT_ALREADY_CURRENT",
                    registered_browsers: browsers,
                    already_current: true,
                    mutation_allowed: true,
                    exit_code: EXIT_OK,
                });
            }
            // The first development slice intentionally has no update or
            // target-reconfiguration transaction. An exact owned uninstall is
            // required before installing different source bytes or browsers.
            return Err(DevelopmentError::ReinstallRequired);
        }

        let binary_created = stage_source_build(&source, &binary, &paths.install_root)?;
        let created_registrations = match create_registrations(&registrations, manifest.as_bytes())
        {
            Ok(created) => created,
            Err(error) => {
                if binary_created
                    && remove_owned_binary(&binary, &source.sha256, source.identity.size).is_err()
                {
                    return Err(DevelopmentError::PartialMutation);
                }
                return Err(error);
            }
        };
        if let Err(error) = create_new_file(&state_path, state.encode().as_bytes(), 0o600) {
            let registrations_clean = remove_created_registrations(
                &created_registrations,
                &state.manifest_sha256,
                manifest.len() as u64,
            )
            .is_ok();
            let binary_clean = !binary_created
                || remove_owned_binary(&binary, &source.sha256, source.identity.size).is_ok();
            if !registrations_clean || !binary_clean {
                return Err(DevelopmentError::PartialMutation);
            }
            return Err(error);
        }
        if verify_owned_state(&state, &paths).is_err() {
            let state_clean = remove_exact_file(
                &state_path,
                &sha256(state.encode().as_bytes()),
                state.encode().len() as u64,
            )
            .is_ok();
            let registrations_clean = remove_created_registrations(
                &created_registrations,
                &state.manifest_sha256,
                manifest.len() as u64,
            )
            .is_ok();
            if binary_created {
                let binary_clean =
                    remove_owned_binary(&binary, &source.sha256, source.identity.size).is_ok();
                if !state_clean || !registrations_clean || !binary_clean {
                    return Err(DevelopmentError::PartialMutation);
                }
            } else if !state_clean || !registrations_clean {
                return Err(DevelopmentError::PartialMutation);
            }
            return Err(DevelopmentError::InstallUnhealthy);
        }
        Ok(DevelopmentResult {
            action: DevelopmentAction::Install,
            state: STATE_INSTALLED,
            reason_code: "DEVELOPMENT_INSTALLED",
            registered_browsers: browsers,
            already_current: false,
            mutation_allowed: true,
            exit_code: EXIT_OK,
        })
    }

    fn update() -> Result<DevelopmentResult, DevelopmentError> {
        require_supported_build()?;
        let platform = Platform::current();
        let paths = paths_for(platform)?;
        let source = open_executing_source_build()?;
        update_from_source(&source, platform, &paths, UpdateFault::None)
    }

    fn update_from_source(
        source: &SourceBuild,
        platform: Platform,
        paths: &UserPaths,
        fault: UpdateFault,
    ) -> Result<DevelopmentResult, DevelopmentError> {
        match fs::symlink_metadata(&paths.install_root) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                return Err(DevelopmentError::UpdateNotInstalled)
            }
            Ok(_) => {}
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        ensure_private_root(&paths.install_root)?;
        let _lock = InstallLock::acquire(&paths.install_root.join("development-install.lock"))?;
        recover_interrupted_update(paths)?;

        let state_path = paths.install_root.join(STATE_FILE);
        let old_state = load_state(&state_path)?.ok_or(DevelopmentError::UpdateNotInstalled)?;
        verify_canonical_owned_state(&old_state, paths)?;

        let binary = binary_path(
            &paths.install_root,
            COMPANION_VERSION,
            platform,
            architecture(),
            &source.sha256,
        );
        let manifest =
            generate_development_manifest(&binary).map_err(|_| DevelopmentError::SourceInvalid)?;
        let registrations = registration_paths(&old_state.registered_browsers, platform, paths)?;
        let new_state = DevelopmentState {
            install_id: old_state.install_id.clone(),
            companion_version: COMPANION_VERSION.to_owned(),
            platform,
            architecture: architecture().to_owned(),
            active_binary_sha256: source.sha256.clone(),
            active_binary_size: source.identity.size,
            registered_browsers: old_state.registered_browsers.clone(),
            registration_path_sha256: old_state.registration_path_sha256.clone(),
            manifest_sha256: sha256(manifest.as_bytes()),
        };
        validate_update_state_pair(&old_state, &new_state, paths)?;
        if old_state == new_state {
            return Ok(DevelopmentResult {
                action: DevelopmentAction::Update,
                state: STATE_INSTALLED,
                reason_code: "DEVELOPMENT_ALREADY_CURRENT",
                registered_browsers: old_state.registered_browsers,
                already_current: true,
                mutation_allowed: true,
                exit_code: EXIT_OK,
            });
        }

        let journal = DevelopmentUpdateJournal {
            transaction_id: random_id()?,
            old_state: old_state.clone(),
            new_state: new_state.clone(),
        };
        validate_update_state_pair(&journal.old_state, &journal.new_state, paths)?;
        let journal_path = paths.install_root.join(UPDATE_JOURNAL_FILE);
        let journal_bytes = journal.encode()?;
        let candidate_stage = prepare_update_source_stage(
            source,
            &binary,
            &paths.install_root,
            &journal.transaction_id,
        )?;
        if let Err(error) = publish_update_journal(
            &journal_path,
            journal_bytes.as_bytes(),
            &journal.transaction_id,
        ) {
            if let Some(stage) = candidate_stage.as_deref() {
                if remove_exact_file(stage, &source.sha256, source.identity.size).is_err() {
                    return Err(DevelopmentError::PartialMutation);
                }
            }
            return Err(error);
        }
        #[cfg(test)]
        if fault == UpdateFault::AfterJournal {
            return Err(DevelopmentError::PartialMutation);
        }
        if let Some(stage) = candidate_stage.as_deref() {
            if let Err(error) =
                publish_update_binary_stage(stage, &binary, &source.sha256, source.identity.size)
            {
                return match recover_interrupted_update(paths) {
                    Ok(_) => Err(error),
                    Err(_) => Err(DevelopmentError::PartialMutation),
                };
            }
        }
        validate_update_journal(&journal, paths)?;

        let apply_result =
            apply_update_switch(paths, &journal, &registrations, manifest.as_bytes(), fault);
        if let Err(error) = apply_result {
            #[cfg(test)]
            if fault == UpdateFault::AfterFirstManifest {
                return Err(error);
            }
            return match recover_interrupted_update(paths) {
                Ok(_) => Err(error),
                Err(_) => Err(DevelopmentError::PartialMutation),
            };
        }

        verify_canonical_owned_state(&new_state, paths)?;
        remove_exact_file(
            &journal_path,
            &sha256(journal_bytes.as_bytes()),
            journal_bytes.len() as u64,
        )
        .map_err(|_| DevelopmentError::PartialMutation)?;
        Ok(DevelopmentResult {
            action: DevelopmentAction::Update,
            state: STATE_INSTALLED,
            reason_code: "DEVELOPMENT_UPDATED",
            registered_browsers: new_state.registered_browsers,
            already_current: false,
            mutation_allowed: true,
            exit_code: EXIT_OK,
        })
    }

    fn apply_update_switch(
        paths: &UserPaths,
        journal: &DevelopmentUpdateJournal,
        registrations: &BTreeMap<PathBuf, BTreeSet<BrowserId>>,
        new_manifest: &[u8],
        fault: UpdateFault,
    ) -> Result<(), DevelopmentError> {
        let old_binary = binary_path(
            &paths.install_root,
            &journal.old_state.companion_version,
            journal.old_state.platform,
            &journal.old_state.architecture,
            &journal.old_state.active_binary_sha256,
        );
        let old_manifest = generate_development_manifest(&old_binary)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        for (index, path) in registrations.keys().enumerate() {
            let stage = update_stage_path(path, &journal.transaction_id, index)?;
            replace_exact_owned_file(
                path,
                &stage,
                old_manifest.as_bytes(),
                new_manifest,
                MAX_MANIFEST_BYTES,
            )?;
            #[cfg(test)]
            if fault == UpdateFault::AfterFirstManifest && index == 0 {
                return Err(DevelopmentError::PartialMutation);
            }
            #[cfg(not(test))]
            let _ = (index, fault);
        }
        let state_path = paths.install_root.join(STATE_FILE);
        let state_stage =
            update_stage_path(&state_path, &journal.transaction_id, registrations.len())?;
        replace_exact_owned_file(
            &state_path,
            &state_stage,
            journal.old_state.encode().as_bytes(),
            journal.new_state.encode().as_bytes(),
            MAX_STATE_BYTES,
        )
    }

    fn status() -> Result<DevelopmentResult, DevelopmentError> {
        require_supported_build()?;
        let platform = Platform::current();
        let paths = paths_for(platform)?;
        let state_path = paths.install_root.join(STATE_FILE);
        match fs::symlink_metadata(paths.install_root.join(UPDATE_JOURNAL_FILE)) {
            Ok(_) => {
                let registered_browsers =
                    load_update_journal(&paths.install_root.join(UPDATE_JOURNAL_FILE), false)
                        .ok()
                        .flatten()
                        .map(|journal| journal.old_state.registered_browsers)
                        .unwrap_or_default();
                return Ok(DevelopmentResult {
                    action: DevelopmentAction::Status,
                    state: STATE_UNHEALTHY,
                    reason_code: "DEVELOPMENT_UPDATE_RECOVERY_REQUIRED",
                    registered_browsers,
                    already_current: false,
                    mutation_allowed: false,
                    exit_code: EXIT_INTEGRITY_OR_POLICY,
                });
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        let Some(state) = load_state(&state_path)? else {
            if any_registration_exists(platform, &paths)? {
                return Ok(DevelopmentResult {
                    action: DevelopmentAction::Status,
                    state: STATE_UNHEALTHY,
                    reason_code: "DEVELOPMENT_UNOWNED_REGISTRATION_PRESENT",
                    registered_browsers: Vec::new(),
                    already_current: false,
                    mutation_allowed: false,
                    exit_code: EXIT_INTEGRITY_OR_POLICY,
                });
            }
            return Ok(DevelopmentResult {
                action: DevelopmentAction::Status,
                state: STATE_NOT_INSTALLED,
                reason_code: "DEVELOPMENT_NOT_INSTALLED",
                registered_browsers: Vec::new(),
                already_current: false,
                mutation_allowed: false,
                exit_code: EXIT_NOT_INSTALLED,
            });
        };
        if verify_owned_state(&state, &paths).is_err() {
            return Ok(DevelopmentResult {
                action: DevelopmentAction::Status,
                state: STATE_UNHEALTHY,
                reason_code: "DEVELOPMENT_INSTALL_UNHEALTHY",
                registered_browsers: state.registered_browsers,
                already_current: false,
                mutation_allowed: false,
                exit_code: EXIT_INTEGRITY_OR_POLICY,
            });
        }
        Ok(DevelopmentResult {
            action: DevelopmentAction::Status,
            state: STATE_INSTALLED,
            reason_code: "DEVELOPMENT_INSTALLED",
            registered_browsers: state.registered_browsers,
            already_current: false,
            mutation_allowed: false,
            exit_code: EXIT_OK,
        })
    }

    fn uninstall() -> Result<DevelopmentResult, DevelopmentError> {
        require_supported_build()?;
        let platform = Platform::current();
        let paths = paths_for(platform)?;
        let _lock = match fs::symlink_metadata(&paths.install_root) {
            Ok(_) => {
                ensure_private_root(&paths.install_root)?;
                let lock =
                    InstallLock::acquire(&paths.install_root.join("development-install.lock"))?;
                require_no_update_journal(&paths)?;
                Some(lock)
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(_) => return Err(DevelopmentError::Filesystem),
        };
        let state_path = paths.install_root.join(STATE_FILE);
        let Some(state) = load_state(&state_path)? else {
            if any_registration_exists(platform, &paths)? {
                return Err(DevelopmentError::RegistrationConflict);
            }
            return Ok(DevelopmentResult {
                action: DevelopmentAction::Uninstall,
                state: STATE_NOT_INSTALLED,
                reason_code: "DEVELOPMENT_NOT_INSTALLED",
                registered_browsers: Vec::new(),
                already_current: true,
                mutation_allowed: true,
                exit_code: EXIT_OK,
            });
        };
        verify_owned_state(&state, &paths)?;
        let prior = registration_snapshot(Some(&state), &paths)?;
        let removed = remove_owned_registrations(&prior, &state.manifest_sha256)?;
        let binary = binary_path(
            &paths.install_root,
            &state.companion_version,
            state.platform,
            &state.architecture,
            &state.active_binary_sha256,
        );
        let tombstone = paths
            .install_root
            .join(format!(".development-uninstall-{}", random_id()?));
        if rename_exact_file(
            &binary,
            &tombstone,
            &state.active_binary_sha256,
            state.active_binary_size,
            true,
        )
        .is_err()
        {
            let _ = restore_removed_registrations(&removed);
            return Err(DevelopmentError::PartialMutation);
        }
        let state_bytes = state.encode();
        if remove_exact_file(
            &state_path,
            &sha256(state_bytes.as_bytes()),
            state_bytes.len() as u64,
        )
        .is_err()
        {
            let _ = rename_exact_file(
                &tombstone,
                &binary,
                &state.active_binary_sha256,
                state.active_binary_size,
                true,
            );
            let _ = restore_removed_registrations(&removed);
            return Err(DevelopmentError::PartialMutation);
        }
        if remove_exact_file(
            &tombstone,
            &state.active_binary_sha256,
            state.active_binary_size,
        )
        .is_err()
        {
            return Ok(DevelopmentResult {
                action: DevelopmentAction::Uninstall,
                state: STATE_NOT_INSTALLED,
                reason_code: "DEVELOPMENT_UNINSTALLED_CLEANUP_PENDING",
                registered_browsers: Vec::new(),
                already_current: false,
                mutation_allowed: true,
                exit_code: EXIT_PARTIAL,
            });
        }
        Ok(DevelopmentResult {
            action: DevelopmentAction::Uninstall,
            state: STATE_NOT_INSTALLED,
            reason_code: "DEVELOPMENT_UNINSTALLED_CREDENTIAL_CLEANUP_PENDING",
            registered_browsers: Vec::new(),
            already_current: false,
            mutation_allowed: true,
            exit_code: EXIT_PARTIAL,
        })
    }

    fn require_supported_build() -> Result<(), DevelopmentError> {
        if NATIVE_HOST_NAME != "io.agentzero.browser_bridge.dev"
            || !matches!(Platform::current(), Platform::Macos | Platform::Linux)
        {
            return Err(DevelopmentError::UnsupportedPlatform);
        }
        Ok(())
    }

    fn paths_for(platform: Platform) -> Result<UserPaths, DevelopmentError> {
        let paths = discover_user_paths().map_err(|_| DevelopmentError::UserPathsUnavailable)?;
        if !paths.install_root.is_absolute()
            || !matches!(platform, Platform::Macos | Platform::Linux)
            || (platform == Platform::Macos
                && paths
                    .home_root
                    .as_ref()
                    .map_or(true, |path| !path.is_absolute()))
            || (platform == Platform::Linux
                && paths
                    .config_root
                    .as_ref()
                    .map_or(true, |path| !path.is_absolute()))
        {
            return Err(DevelopmentError::UserPathsUnavailable);
        }
        Ok(paths)
    }

    fn open_executing_source_build() -> Result<SourceBuild, DevelopmentError> {
        let path = std::env::current_exe().map_err(|_| DevelopmentError::SourceInvalid)?;
        if !path.is_absolute() {
            return Err(DevelopmentError::SourceInvalid);
        }
        let path_metadata =
            fs::symlink_metadata(&path).map_err(|_| DevelopmentError::SourceInvalid)?;
        if path_metadata.file_type().is_symlink()
            || !path_metadata.is_file()
            || path_metadata.permissions().mode() & 0o111 == 0
        {
            return Err(DevelopmentError::SourceInvalid);
        }
        let mut file = open_nofollow_regular(&path).map_err(|_| DevelopmentError::SourceInvalid)?;
        let identity = file_identity(&file)?;
        if identity.size == 0 || identity.size > MAX_BINARY_BYTES {
            return Err(DevelopmentError::SourceInvalid);
        }
        if identity.device != path_metadata.dev()
            || identity.inode != path_metadata.ino()
            || identity.size != path_metadata.len()
        {
            return Err(DevelopmentError::SourceInvalid);
        }
        let (sha256, size) = hash_reader(&mut file, MAX_BINARY_BYTES)?;
        if size != identity.size || file_identity(&file)? != identity {
            return Err(DevelopmentError::SourceInvalid);
        }
        file.seek(SeekFrom::Start(0))
            .map_err(|_| DevelopmentError::SourceInvalid)?;
        Ok(SourceBuild {
            file,
            identity,
            sha256,
        })
    }

    fn registration_paths(
        browsers: &[BrowserId],
        platform: Platform,
        paths: &UserPaths,
    ) -> Result<BTreeMap<PathBuf, BTreeSet<BrowserId>>, DevelopmentError> {
        let mut result = BTreeMap::<PathBuf, BTreeSet<BrowserId>>::new();
        for browser in browsers {
            let Some(RegistrationLocation::ManifestFiles(files)) =
                registration_location(*browser, platform, paths)
            else {
                return Err(DevelopmentError::RegistrationUnavailable);
            };
            for path in files {
                if !path.is_absolute() {
                    return Err(DevelopmentError::RegistrationUnavailable);
                }
                result.entry(path).or_default().insert(*browser);
            }
        }
        if result.is_empty() || result.len() > BROWSERS.len() + 1 {
            return Err(DevelopmentError::RegistrationUnavailable);
        }
        Ok(result)
    }

    fn validate_registration_collisions(
        old_state: Option<&DevelopmentState>,
        desired: &BTreeMap<PathBuf, BTreeSet<BrowserId>>,
        paths: &UserPaths,
    ) -> Result<(), DevelopmentError> {
        let owned = registration_snapshot(old_state, paths)?;
        for path in desired.keys() {
            match fs::symlink_metadata(path) {
                Ok(_) if !owned.contains_key(path) => {
                    return Err(DevelopmentError::RegistrationConflict)
                }
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(_) => return Err(DevelopmentError::Filesystem),
            }
        }
        Ok(())
    }

    fn registration_snapshot(
        state: Option<&DevelopmentState>,
        paths: &UserPaths,
    ) -> Result<BTreeMap<PathBuf, Vec<u8>>, DevelopmentError> {
        let Some(state) = state else {
            return Ok(BTreeMap::new());
        };
        let paths_by_browser =
            registration_paths(&state.registered_browsers, state.platform, paths)?;
        let mut result = BTreeMap::new();
        for path in paths_by_browser.keys() {
            let bytes = read_regular_bounded(path, MAX_MANIFEST_BYTES)?
                .ok_or(DevelopmentError::InstallUnhealthy)?;
            if sha256(&bytes) != state.manifest_sha256 {
                return Err(DevelopmentError::InstallUnhealthy);
            }
            result.insert(path.clone(), bytes);
        }
        Ok(result)
    }

    fn create_registrations(
        desired: &BTreeMap<PathBuf, BTreeSet<BrowserId>>,
        manifest: &[u8],
    ) -> Result<Vec<PathBuf>, DevelopmentError> {
        let digest = sha256(manifest);
        let mut created = Vec::new();
        for path in desired.keys() {
            if let Err(error) = create_new_file(path, manifest, 0o600) {
                if remove_created_registrations(&created, &digest, manifest.len() as u64).is_err() {
                    return Err(DevelopmentError::PartialMutation);
                }
                return Err(error);
            }
            created.push(path.clone());
        }
        Ok(created)
    }

    fn remove_created_registrations(
        created: &[PathBuf],
        expected_sha256: &str,
        expected_size: u64,
    ) -> Result<(), DevelopmentError> {
        for path in created.iter().rev() {
            remove_exact_file(path, expected_sha256, expected_size)?;
        }
        Ok(())
    }

    fn remove_owned_registrations(
        prior: &BTreeMap<PathBuf, Vec<u8>>,
        expected_sha256: &str,
    ) -> Result<Vec<(PathBuf, Vec<u8>)>, DevelopmentError> {
        let mut removed = Vec::new();
        for (path, bytes) in prior {
            if sha256(bytes) != expected_sha256 {
                return Err(DevelopmentError::InstallUnhealthy);
            }
            if let Err(error) = remove_exact_file(path, expected_sha256, bytes.len() as u64) {
                if restore_removed_registrations(&removed).is_err() {
                    return Err(DevelopmentError::PartialMutation);
                }
                return Err(error);
            }
            removed.push((path.clone(), bytes.clone()));
        }
        Ok(removed)
    }

    fn restore_removed_registrations(
        removed: &[(PathBuf, Vec<u8>)],
    ) -> Result<(), DevelopmentError> {
        for (path, bytes) in removed.iter().rev() {
            create_new_file(path, bytes, 0o600)?;
        }
        Ok(())
    }

    fn verify_owned_state(
        state: &DevelopmentState,
        paths: &UserPaths,
    ) -> Result<(), DevelopmentError> {
        if state.platform != Platform::current() || state.architecture != architecture() {
            return Err(DevelopmentError::StateInvalid);
        }
        let binary = binary_path(
            &paths.install_root,
            &state.companion_version,
            state.platform,
            &state.architecture,
            &state.active_binary_sha256,
        );
        verify_regular_digest(
            &binary,
            &state.active_binary_sha256,
            state.active_binary_size,
            true,
        )?;
        let manifest =
            generate_development_manifest(&binary).map_err(|_| DevelopmentError::StateInvalid)?;
        if sha256(manifest.as_bytes()) != state.manifest_sha256 {
            return Err(DevelopmentError::StateInvalid);
        }
        let registrations = registration_paths(&state.registered_browsers, state.platform, paths)?;
        let expected_path_digests = registrations
            .keys()
            .map(|path| path_bytes(path).map(|bytes| sha256(&bytes)))
            .collect::<Result<Vec<_>, DevelopmentError>>()?;
        if expected_path_digests != state.registration_path_sha256 {
            return Err(DevelopmentError::StateInvalid);
        }
        registration_snapshot(Some(state), paths)?;
        Ok(())
    }

    fn verify_canonical_owned_state(
        state: &DevelopmentState,
        paths: &UserPaths,
    ) -> Result<(), DevelopmentError> {
        verify_owned_state(state, paths)?;
        let state_path = paths.install_root.join(STATE_FILE);
        if read_regular_bounded(&state_path, MAX_STATE_BYTES)?.as_deref()
            != Some(state.encode().as_bytes())
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let binary = binary_path(
            &paths.install_root,
            &state.companion_version,
            state.platform,
            &state.architecture,
            &state.active_binary_sha256,
        );
        let manifest =
            generate_development_manifest(&binary).map_err(|_| DevelopmentError::StateInvalid)?;
        let registrations = registration_paths(&state.registered_browsers, state.platform, paths)?;
        for path in registrations.keys() {
            if read_regular_bounded(path, MAX_MANIFEST_BYTES)?.as_deref()
                != Some(manifest.as_bytes())
            {
                return Err(DevelopmentError::InstallUnhealthy);
            }
        }
        Ok(())
    }

    fn any_registration_exists(
        platform: Platform,
        paths: &UserPaths,
    ) -> Result<bool, DevelopmentError> {
        for browser in BROWSERS {
            let Some(RegistrationLocation::ManifestFiles(files)) =
                registration_location(browser.id, platform, paths)
            else {
                continue;
            };
            for path in files {
                match fs::symlink_metadata(path) {
                    Ok(_) => return Ok(true),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(_) => return Err(DevelopmentError::Filesystem),
                }
            }
        }
        Ok(false)
    }

    fn stage_source_build(
        source: &SourceBuild,
        binary: &Path,
        install_root: &Path,
    ) -> Result<bool, DevelopmentError> {
        match fs::symlink_metadata(binary) {
            Ok(_) => {
                verify_regular_digest(binary, &source.sha256, source.identity.size, true)?;
                return Ok(false);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        let parent = binary.parent().ok_or(DevelopmentError::Filesystem)?;
        if !parent.starts_with(install_root) {
            return Err(DevelopmentError::Filesystem);
        }
        ensure_directory_chain(parent, Some(install_root))?;
        let temp = parent.join(format!(".source-{}.tmp", random_id()?));
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)
            .map_err(|_| DevelopmentError::Filesystem)?;
        let mut linked = false;
        let result = (|| {
            let mut input = source
                .file
                .try_clone()
                .map_err(|_| DevelopmentError::SourceInvalid)?;
            input
                .seek(SeekFrom::Start(0))
                .map_err(|_| DevelopmentError::SourceInvalid)?;
            let copied = io::copy(
                &mut std::io::Read::by_ref(&mut input).take(MAX_BINARY_BYTES + 1),
                &mut output,
            )
            .map_err(|_| DevelopmentError::Filesystem)?;
            if copied != source.identity.size || copied > MAX_BINARY_BYTES {
                return Err(DevelopmentError::SourceInvalid);
            }
            output
                .sync_all()
                .map_err(|_| DevelopmentError::Filesystem)?;
            fs::set_permissions(&temp, fs::Permissions::from_mode(0o500))
                .map_err(|_| DevelopmentError::Filesystem)?;
            verify_regular_digest(&temp, &source.sha256, source.identity.size, true)?;
            fs::hard_link(&temp, binary).map_err(|error| {
                if error.kind() == io::ErrorKind::AlreadyExists {
                    DevelopmentError::RegistrationConflict
                } else {
                    DevelopmentError::Filesystem
                }
            })?;
            linked = true;
            fs::remove_file(&temp).map_err(|_| DevelopmentError::Filesystem)?;
            sync_parent(binary)?;
            verify_regular_digest(binary, &source.sha256, source.identity.size, true)
        })();
        if result.is_err() {
            let temp_clean = match fs::symlink_metadata(&temp) {
                Err(error) if error.kind() == io::ErrorKind::NotFound => true,
                Ok(_) => remove_exact_file(&temp, &source.sha256, source.identity.size).is_ok(),
                Err(_) => false,
            };
            if linked && remove_owned_binary(binary, &source.sha256, source.identity.size).is_err()
            {
                return Err(DevelopmentError::PartialMutation);
            }
            if !temp_clean {
                return Err(DevelopmentError::PartialMutation);
            }
        }
        result.map(|()| true)
    }

    fn remove_owned_binary(
        binary: &Path,
        expected_sha256: &str,
        expected_size: u64,
    ) -> Result<(), DevelopmentError> {
        remove_exact_file(binary, expected_sha256, expected_size)?;
        let mut current = binary.parent();
        for _ in 0..3 {
            let Some(directory) = current else { break };
            match fs::remove_dir(directory) {
                Ok(()) => current = directory.parent(),
                Err(error) if error.kind() == io::ErrorKind::DirectoryNotEmpty => break,
                Err(_) => break,
            }
        }
        Ok(())
    }

    fn binary_path(
        root: &Path,
        version: &str,
        platform: Platform,
        architecture: &str,
        sha256: &str,
    ) -> PathBuf {
        root.join("releases")
            .join(version)
            .join(format!("{}-{architecture}", platform.as_str()))
            .join(sha256)
            .join("a0-browser-bridge")
    }

    fn load_state(path: &Path) -> Result<Option<DevelopmentState>, DevelopmentError> {
        let Some(bytes) = read_regular_bounded(path, MAX_STATE_BYTES)? else {
            return Ok(None);
        };
        let value = json::parse(&bytes).map_err(|_| DevelopmentError::StateInvalid)?;
        parse_state(&value).map(Some)
    }

    fn parse_state(value: &Value) -> Result<DevelopmentState, DevelopmentError> {
        let fields = exact_object(
            value,
            &[
                "contract",
                "schema_version",
                "channel",
                "install_id",
                "companion_version",
                "native_host_name",
                "extension_origin",
                "trust_contract",
                "platform",
                "architecture",
                "active_binary_sha256",
                "active_binary_size",
                "registered_browsers",
                "registration_path_sha256",
                "manifest_sha256",
                "source",
            ],
        )?;
        if string(fields, "contract")? != STATE_CONTRACT
            || number(fields, "schema_version")? != 1
            || string(fields, "channel")? != DEVELOPMENT_CHANNEL
            || string(fields, "native_host_name")? != NATIVE_HOST_NAME
            || string(fields, "extension_origin")? != DEVELOPMENT_EXTENSION_ORIGIN
            || string(fields, "trust_contract")? != DEVELOPMENT_TRUST_CONTRACT
            || string(fields, "source")? != "executing-local-source-build"
        {
            return Err(DevelopmentError::StateInvalid);
        }
        let install_id = string(fields, "install_id")?.to_owned();
        let companion_version = string(fields, "companion_version")?.to_owned();
        let platform = match string(fields, "platform")? {
            "macos" => Platform::Macos,
            "linux" => Platform::Linux,
            _ => return Err(DevelopmentError::StateInvalid),
        };
        let architecture = string(fields, "architecture")?.to_owned();
        let active_binary_sha256 = string(fields, "active_binary_sha256")?.to_owned();
        let active_binary_size = number(fields, "active_binary_size")?;
        let registered_browsers = string_array(fields, "registered_browsers")?
            .into_iter()
            .map(|value| BrowserId::parse(&value).ok_or(DevelopmentError::StateInvalid))
            .collect::<Result<Vec<_>, _>>()?;
        let registration_path_sha256 = string_array(fields, "registration_path_sha256")?;
        let manifest_sha256 = string(fields, "manifest_sha256")?.to_owned();
        if !valid_id(&install_id)
            || !valid_version(&companion_version)
            || !matches!(architecture.as_str(), "x86_64" | "aarch64")
            || !valid_sha256(&active_binary_sha256)
            || active_binary_size == 0
            || active_binary_size > MAX_BINARY_BYTES
            || registered_browsers.is_empty()
            || registered_browsers.len() > BROWSERS.len()
            || registered_browsers
                .iter()
                .copied()
                .collect::<BTreeSet<_>>()
                .len()
                != registered_browsers.len()
            || !registered_browsers.windows(2).all(|pair| pair[0] < pair[1])
            || registration_path_sha256.is_empty()
            || registration_path_sha256.len() > BROWSERS.len() + 1
            || registration_path_sha256
                .iter()
                .any(|value| !valid_sha256(value))
            || registration_path_sha256
                .iter()
                .collect::<BTreeSet<_>>()
                .len()
                != registration_path_sha256.len()
            || !valid_sha256(&manifest_sha256)
        {
            return Err(DevelopmentError::StateInvalid);
        }
        Ok(DevelopmentState {
            install_id,
            companion_version,
            platform,
            architecture,
            active_binary_sha256,
            active_binary_size,
            registered_browsers,
            registration_path_sha256,
            manifest_sha256,
        })
    }

    fn load_update_journal(
        path: &Path,
        normalize_publication: bool,
    ) -> Result<Option<DevelopmentUpdateJournal>, DevelopmentError> {
        let Some(snapshot) = read_owned_snapshot(path, MAX_UPDATE_JOURNAL_BYTES)? else {
            return Ok(None);
        };
        let value =
            json::parse(&snapshot.bytes).map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        let journal = parse_update_journal(&value)?;
        if journal
            .encode()
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?
            .as_bytes()
            != snapshot.bytes
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        if snapshot.links == 2 {
            let stage = journal_publication_stage_path(path, &journal.transaction_id)?;
            let linked = read_owned_snapshot(&stage, MAX_UPDATE_JOURNAL_BYTES)?
                .ok_or(DevelopmentError::UpdateRecoveryRequired)?;
            if linked.links != 2
                || linked.device != snapshot.device
                || linked.inode != snapshot.inode
                || linked.bytes != snapshot.bytes
            {
                return Err(DevelopmentError::UpdateRecoveryRequired);
            }
            if normalize_publication {
                fs::remove_file(&stage).map_err(|_| DevelopmentError::PartialMutation)?;
                sync_parent(&stage).map_err(|_| DevelopmentError::PartialMutation)?;
                if read_regular_bounded(path, MAX_UPDATE_JOURNAL_BYTES)?.as_deref()
                    != Some(snapshot.bytes.as_slice())
                {
                    return Err(DevelopmentError::PartialMutation);
                }
            }
        } else if snapshot.links != 1 {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        Ok(Some(journal))
    }

    fn require_no_update_journal(paths: &UserPaths) -> Result<(), DevelopmentError> {
        match fs::symlink_metadata(paths.install_root.join(UPDATE_JOURNAL_FILE)) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Ok(_) => Err(DevelopmentError::UpdateRecoveryRequired),
            Err(_) => Err(DevelopmentError::Filesystem),
        }
    }

    fn parse_update_journal(value: &Value) -> Result<DevelopmentUpdateJournal, DevelopmentError> {
        let fields = exact_object(
            value,
            &[
                "contract",
                "schema_version",
                "transaction_id",
                "old_state",
                "new_state",
            ],
        )
        .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        if string(fields, "contract").map_err(|_| DevelopmentError::UpdateRecoveryRequired)?
            != UPDATE_JOURNAL_CONTRACT
            || number(fields, "schema_version")
                .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?
                != 1
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        let transaction_id = string(fields, "transaction_id")
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?
            .to_owned();
        let old_state = fields
            .get("old_state")
            .ok_or(DevelopmentError::UpdateRecoveryRequired)
            .and_then(|value| {
                parse_state(value).map_err(|_| DevelopmentError::UpdateRecoveryRequired)
            })?;
        let new_state = fields
            .get("new_state")
            .ok_or(DevelopmentError::UpdateRecoveryRequired)
            .and_then(|value| {
                parse_state(value).map_err(|_| DevelopmentError::UpdateRecoveryRequired)
            })?;
        if !valid_id(&transaction_id) {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        Ok(DevelopmentUpdateJournal {
            transaction_id,
            old_state,
            new_state,
        })
    }

    fn validate_update_state_pair(
        old_state: &DevelopmentState,
        new_state: &DevelopmentState,
        paths: &UserPaths,
    ) -> Result<(), DevelopmentError> {
        if old_state.install_id != new_state.install_id
            || old_state.platform != new_state.platform
            || old_state.architecture != new_state.architecture
            || old_state.registered_browsers != new_state.registered_browsers
            || old_state.registration_path_sha256 != new_state.registration_path_sha256
            || old_state.platform != Platform::current()
            || old_state.architecture != architecture()
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        let registrations =
            registration_paths(&old_state.registered_browsers, old_state.platform, paths)
                .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        let path_digests = registrations
            .keys()
            .map(|path| path_bytes(path).map(|bytes| sha256(&bytes)))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        if path_digests != old_state.registration_path_sha256 {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        for state in [old_state, new_state] {
            let binary = binary_path(
                &paths.install_root,
                &state.companion_version,
                state.platform,
                &state.architecture,
                &state.active_binary_sha256,
            );
            let manifest = generate_development_manifest(&binary)
                .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
            if sha256(manifest.as_bytes()) != state.manifest_sha256 {
                return Err(DevelopmentError::UpdateRecoveryRequired);
            }
        }
        Ok(())
    }

    fn validate_update_journal(
        journal: &DevelopmentUpdateJournal,
        paths: &UserPaths,
    ) -> Result<(), DevelopmentError> {
        validate_update_state_pair(&journal.old_state, &journal.new_state, paths)?;
        if journal.old_state == journal.new_state {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        for state in [&journal.old_state, &journal.new_state] {
            let binary = binary_path(
                &paths.install_root,
                &state.companion_version,
                state.platform,
                &state.architecture,
                &state.active_binary_sha256,
            );
            verify_regular_digest(
                &binary,
                &state.active_binary_sha256,
                state.active_binary_size,
                true,
            )
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        }
        Ok(())
    }

    fn recover_interrupted_update(paths: &UserPaths) -> Result<Option<bool>, DevelopmentError> {
        let journal_path = paths.install_root.join(UPDATE_JOURNAL_FILE);
        let Some(journal) = load_update_journal(&journal_path, true)? else {
            return Ok(None);
        };
        validate_update_state_pair(&journal.old_state, &journal.new_state, paths)?;
        if journal.old_state == journal.new_state {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        let old_binary = binary_path(
            &paths.install_root,
            &journal.old_state.companion_version,
            journal.old_state.platform,
            &journal.old_state.architecture,
            &journal.old_state.active_binary_sha256,
        );
        verify_regular_digest(
            &old_binary,
            &journal.old_state.active_binary_sha256,
            journal.old_state.active_binary_size,
            true,
        )
        .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        let candidate_ready = normalize_update_binary_stage(&journal, paths)?;
        let targets = update_targets(&journal, paths)?;
        // Validate every journal-derived target and deterministic stage before
        // cleaning any of them. Only a partial/full old/new value or the exact
        // two-link publication state is recoverable.
        for target in &targets {
            preflight_update_target(target)?;
        }
        for target in &targets {
            cleanup_update_stage(target)?;
        }

        let state_target = targets
            .last()
            .ok_or(DevelopmentError::UpdateRecoveryRequired)?;
        let current_state = read_regular_bounded(&state_target.path, state_target.max_bytes)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        let committed = match current_state.as_deref() {
            Some(bytes) if bytes == state_target.new.as_slice() => true,
            Some(bytes) if bytes == state_target.old.as_slice() => false,
            None => false,
            _ => return Err(DevelopmentError::UpdateRecoveryRequired),
        };
        if committed && !candidate_ready {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        for target in &targets {
            preflight_known_target(target)?;
        }
        for target in &targets {
            let desired = if committed { &target.new } else { &target.old };
            set_known_file(target, desired)?;
        }
        verify_canonical_owned_state(
            if committed {
                &journal.new_state
            } else {
                &journal.old_state
            },
            paths,
        )?;
        let journal_bytes = journal.encode()?;
        remove_exact_file(
            &journal_path,
            &sha256(journal_bytes.as_bytes()),
            journal_bytes.len() as u64,
        )
        .map_err(|_| DevelopmentError::PartialMutation)?;
        Ok(Some(committed))
    }

    fn journal_publication_stage_path(
        journal_path: &Path,
        transaction_id: &str,
    ) -> Result<PathBuf, DevelopmentError> {
        if !valid_id(transaction_id) {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        Ok(journal_path
            .parent()
            .ok_or(DevelopmentError::UpdateRecoveryRequired)?
            .join(format!(
                ".a0-browser-bridge-development-update-{transaction_id}-journal.tmp"
            )))
    }

    fn publish_update_journal(
        journal_path: &Path,
        bytes: &[u8],
        transaction_id: &str,
    ) -> Result<(), DevelopmentError> {
        let stage = journal_publication_stage_path(journal_path, transaction_id)?;
        create_new_file(&stage, bytes, 0o600)?;
        publish_update_stage(&stage, journal_path, bytes, MAX_UPDATE_JOURNAL_BYTES)
    }

    fn update_binary_stage_path(
        binary: &Path,
        transaction_id: &str,
    ) -> Result<PathBuf, DevelopmentError> {
        if !valid_id(transaction_id) {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        Ok(binary
            .parent()
            .ok_or(DevelopmentError::UpdateRecoveryRequired)?
            .join(format!(
                ".a0-browser-bridge-development-update-{transaction_id}-binary.tmp"
            )))
    }

    fn prepare_update_source_stage(
        source: &SourceBuild,
        binary: &Path,
        install_root: &Path,
        transaction_id: &str,
    ) -> Result<Option<PathBuf>, DevelopmentError> {
        match fs::symlink_metadata(binary) {
            Ok(_) => {
                verify_regular_digest(binary, &source.sha256, source.identity.size, true)?;
                return Ok(None);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        let parent = binary.parent().ok_or(DevelopmentError::Filesystem)?;
        if !parent.starts_with(install_root) {
            return Err(DevelopmentError::Filesystem);
        }
        ensure_directory_chain(parent, Some(install_root))?;
        let stage = update_binary_stage_path(binary, transaction_id)?;
        match fs::symlink_metadata(&stage) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Ok(_) => return Err(DevelopmentError::UpdateRecoveryRequired),
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&stage)
            .map_err(|_| DevelopmentError::Filesystem)?;
        let result = (|| {
            let mut input = source
                .file
                .try_clone()
                .map_err(|_| DevelopmentError::SourceInvalid)?;
            input
                .seek(SeekFrom::Start(0))
                .map_err(|_| DevelopmentError::SourceInvalid)?;
            let copied = io::copy(
                &mut std::io::Read::by_ref(&mut input).take(MAX_BINARY_BYTES + 1),
                &mut output,
            )
            .map_err(|_| DevelopmentError::Filesystem)?;
            if copied != source.identity.size || copied > MAX_BINARY_BYTES {
                return Err(DevelopmentError::SourceInvalid);
            }
            output
                .sync_all()
                .map_err(|_| DevelopmentError::Filesystem)?;
            fs::set_permissions(&stage, fs::Permissions::from_mode(0o500))
                .map_err(|_| DevelopmentError::Filesystem)?;
            verify_regular_digest(&stage, &source.sha256, source.identity.size, true)
        })();
        if let Err(error) = result {
            return if remove_created_file_by_identity(&stage, &output).is_ok() {
                Err(error)
            } else {
                Err(DevelopmentError::PartialMutation)
            };
        }
        Ok(Some(stage))
    }

    fn publish_update_binary_stage(
        stage: &Path,
        binary: &Path,
        expected_sha256: &str,
        expected_size: u64,
    ) -> Result<(), DevelopmentError> {
        verify_regular_digest(stage, expected_sha256, expected_size, true)?;
        match fs::symlink_metadata(binary) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Ok(_) => return Err(DevelopmentError::RegistrationConflict),
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        fs::hard_link(stage, binary).map_err(|error| {
            if error.kind() == io::ErrorKind::AlreadyExists {
                DevelopmentError::RegistrationConflict
            } else {
                DevelopmentError::PartialMutation
            }
        })?;
        let stage_metadata =
            fs::symlink_metadata(stage).map_err(|_| DevelopmentError::PartialMutation)?;
        let binary_metadata =
            fs::symlink_metadata(binary).map_err(|_| DevelopmentError::PartialMutation)?;
        if stage_metadata.nlink() != 2
            || binary_metadata.nlink() != 2
            || stage_metadata.dev() != binary_metadata.dev()
            || stage_metadata.ino() != binary_metadata.ino()
        {
            return Err(DevelopmentError::PartialMutation);
        }
        fs::remove_file(stage).map_err(|_| DevelopmentError::PartialMutation)?;
        sync_parent(stage).map_err(|_| DevelopmentError::PartialMutation)?;
        verify_regular_digest(binary, expected_sha256, expected_size, true)
            .map_err(|_| DevelopmentError::PartialMutation)
    }

    fn normalize_update_binary_stage(
        journal: &DevelopmentUpdateJournal,
        paths: &UserPaths,
    ) -> Result<bool, DevelopmentError> {
        let state = &journal.new_state;
        let binary = binary_path(
            &paths.install_root,
            &state.companion_version,
            state.platform,
            &state.architecture,
            &state.active_binary_sha256,
        );
        let stage = update_binary_stage_path(&binary, &journal.transaction_id)?;
        let stage_metadata = match fs::symlink_metadata(&stage) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(_) => return Err(DevelopmentError::UpdateRecoveryRequired),
        };
        let binary_metadata = match fs::symlink_metadata(&binary) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(_) => return Err(DevelopmentError::UpdateRecoveryRequired),
        };
        match (stage_metadata, binary_metadata) {
            (None, None) => Ok(false),
            (None, Some(_)) => {
                verify_regular_digest(
                    &binary,
                    &state.active_binary_sha256,
                    state.active_binary_size,
                    true,
                )
                .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
                Ok(true)
            }
            (Some(stage_meta), None) => {
                if stage_meta.nlink() != 1 {
                    return Err(DevelopmentError::UpdateRecoveryRequired);
                }
                verify_regular_digest(
                    &stage,
                    &state.active_binary_sha256,
                    state.active_binary_size,
                    true,
                )
                .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
                remove_exact_file(
                    &stage,
                    &state.active_binary_sha256,
                    state.active_binary_size,
                )
                .map_err(|_| DevelopmentError::PartialMutation)?;
                Ok(false)
            }
            (Some(stage_meta), Some(binary_meta)) => {
                if stage_meta.nlink() != 2
                    || binary_meta.nlink() != 2
                    || stage_meta.dev() != binary_meta.dev()
                    || stage_meta.ino() != binary_meta.ino()
                {
                    return Err(DevelopmentError::UpdateRecoveryRequired);
                }
                verify_regular_digest_allowing_links(
                    &stage,
                    &state.active_binary_sha256,
                    state.active_binary_size,
                    true,
                    2,
                )?;
                fs::remove_file(&stage).map_err(|_| DevelopmentError::PartialMutation)?;
                sync_parent(&stage).map_err(|_| DevelopmentError::PartialMutation)?;
                verify_regular_digest(
                    &binary,
                    &state.active_binary_sha256,
                    state.active_binary_size,
                    true,
                )
                .map_err(|_| DevelopmentError::PartialMutation)?;
                Ok(true)
            }
        }
    }

    fn update_targets(
        journal: &DevelopmentUpdateJournal,
        paths: &UserPaths,
    ) -> Result<Vec<UpdateTarget>, DevelopmentError> {
        let old_binary = binary_path(
            &paths.install_root,
            &journal.old_state.companion_version,
            journal.old_state.platform,
            &journal.old_state.architecture,
            &journal.old_state.active_binary_sha256,
        );
        let new_binary = binary_path(
            &paths.install_root,
            &journal.new_state.companion_version,
            journal.new_state.platform,
            &journal.new_state.architecture,
            &journal.new_state.active_binary_sha256,
        );
        let old_manifest = generate_development_manifest(&old_binary)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?
            .into_bytes();
        let new_manifest = generate_development_manifest(&new_binary)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?
            .into_bytes();
        let registrations = registration_paths(
            &journal.old_state.registered_browsers,
            journal.old_state.platform,
            paths,
        )
        .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        let mut targets = Vec::with_capacity(registrations.len() + 1);
        for (index, path) in registrations.keys().enumerate() {
            targets.push(UpdateTarget {
                path: path.clone(),
                stage: update_stage_path(path, &journal.transaction_id, index)?,
                old: old_manifest.clone(),
                new: new_manifest.clone(),
                max_bytes: MAX_MANIFEST_BYTES,
            });
        }
        let state_path = paths.install_root.join(STATE_FILE);
        targets.push(UpdateTarget {
            stage: update_stage_path(&state_path, &journal.transaction_id, targets.len())?,
            path: state_path,
            old: journal.old_state.encode().into_bytes(),
            new: journal.new_state.encode().into_bytes(),
            max_bytes: MAX_STATE_BYTES,
        });
        Ok(targets)
    }

    fn update_stage_path(
        target: &Path,
        transaction_id: &str,
        index: usize,
    ) -> Result<PathBuf, DevelopmentError> {
        if !valid_id(transaction_id) {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        let parent = target
            .parent()
            .ok_or(DevelopmentError::UpdateRecoveryRequired)?;
        Ok(parent.join(format!(
            ".a0-browser-bridge-development-update-{transaction_id}-{index}.tmp"
        )))
    }

    fn read_owned_snapshot(
        path: &Path,
        max_bytes: usize,
    ) -> Result<Option<OwnedFileSnapshot>, DevelopmentError> {
        let path_metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(DevelopmentError::UpdateRecoveryRequired),
        };
        if path_metadata.file_type().is_symlink()
            || !path_metadata.is_file()
            || path_metadata.uid() != effective_uid()
            || !matches!(path_metadata.nlink(), 1 | 2)
            || path_metadata.len() > max_bytes as u64
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(open_nofollow_flag())
            .open(path)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        let opened = file
            .metadata()
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        if !opened.is_file()
            || opened.uid() != effective_uid()
            || opened.dev() != path_metadata.dev()
            || opened.ino() != path_metadata.ino()
            || opened.len() != path_metadata.len()
            || opened.nlink() != path_metadata.nlink()
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        let mut bytes = Vec::with_capacity(opened.len() as usize);
        file.take((max_bytes + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        if bytes.len() > max_bytes || bytes.len() as u64 != opened.len() {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        Ok(Some(OwnedFileSnapshot {
            device: opened.dev(),
            inode: opened.ino(),
            links: opened.nlink(),
            bytes,
        }))
    }

    fn preflight_update_target(target: &UpdateTarget) -> Result<(), DevelopmentError> {
        let current = read_owned_snapshot(&target.path, target.max_bytes)?;
        let stage = read_owned_snapshot(&target.stage, target.max_bytes)?;
        if current
            .as_ref()
            .is_some_and(|value| value.bytes != target.old && value.bytes != target.new)
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        match (current.as_ref(), stage.as_ref()) {
            (Some(current), Some(stage)) if stage.links == 2 => {
                if current.links != 2
                    || current.device != stage.device
                    || current.inode != stage.inode
                    || current.bytes != stage.bytes
                    || (stage.bytes != target.old && stage.bytes != target.new)
                {
                    return Err(DevelopmentError::UpdateRecoveryRequired);
                }
            }
            (current, Some(stage)) if stage.links == 1 => {
                if current.is_some_and(|value| value.links != 1)
                    || (!target.old.starts_with(&stage.bytes)
                        && !target.new.starts_with(&stage.bytes))
                {
                    return Err(DevelopmentError::UpdateRecoveryRequired);
                }
            }
            (Some(current), None) if current.links != 1 => {
                return Err(DevelopmentError::UpdateRecoveryRequired)
            }
            (None, Some(stage)) if stage.links != 1 => {
                return Err(DevelopmentError::UpdateRecoveryRequired)
            }
            _ => {}
        }
        Ok(())
    }

    fn cleanup_update_stage(target: &UpdateTarget) -> Result<(), DevelopmentError> {
        preflight_update_target(target)?;
        let Some(stage) = read_owned_snapshot(&target.stage, target.max_bytes)? else {
            return Ok(());
        };
        if stage.links == 2 {
            let current = read_owned_snapshot(&target.path, target.max_bytes)?
                .ok_or(DevelopmentError::UpdateRecoveryRequired)?;
            if current.links != 2
                || current.device != stage.device
                || current.inode != stage.inode
                || current.bytes != stage.bytes
            {
                return Err(DevelopmentError::UpdateRecoveryRequired);
            }
            fs::remove_file(&target.stage).map_err(|_| DevelopmentError::PartialMutation)?;
            sync_parent(&target.stage).map_err(|_| DevelopmentError::PartialMutation)?;
            if read_regular_bounded(&target.path, target.max_bytes)?.as_deref()
                != Some(stage.bytes.as_slice())
            {
                return Err(DevelopmentError::PartialMutation);
            }
            return Ok(());
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(open_nofollow_flag())
            .open(&target.stage)
            .map_err(|_| DevelopmentError::PartialMutation)?;
        let expected = if target.old.starts_with(&stage.bytes) {
            &target.old
        } else if target.new.starts_with(&stage.bytes) {
            &target.new
        } else {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        };
        remove_created_prefix(&target.stage, &mut file, expected)
            .map_err(|_| DevelopmentError::PartialMutation)
    }

    fn preflight_known_target(target: &UpdateTarget) -> Result<(), DevelopmentError> {
        let current = read_regular_bounded(&target.path, target.max_bytes)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        if current
            .as_deref()
            .is_some_and(|bytes| bytes != target.old && bytes != target.new)
        {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        Ok(())
    }

    fn set_known_file(target: &UpdateTarget, desired: &[u8]) -> Result<(), DevelopmentError> {
        let current = read_regular_bounded(&target.path, target.max_bytes)
            .map_err(|_| DevelopmentError::UpdateRecoveryRequired)?;
        match current.as_deref() {
            Some(bytes) if bytes == desired => return Ok(()),
            Some(bytes) if bytes == target.old || bytes == target.new => {}
            None => {}
            _ => return Err(DevelopmentError::UpdateRecoveryRequired),
        }
        prepare_update_stage(&target.stage, desired)?;
        if let Some(bytes) = current.as_deref() {
            if read_regular_bounded(&target.path, target.max_bytes)?.as_deref() != Some(bytes) {
                return Err(DevelopmentError::UpdateRecoveryRequired);
            }
            remove_exact_file(&target.path, &sha256(bytes), bytes.len() as u64)
                .map_err(|_| DevelopmentError::PartialMutation)?;
        } else if fs::symlink_metadata(&target.path).is_ok() {
            return Err(DevelopmentError::UpdateRecoveryRequired);
        }
        publish_update_stage(&target.stage, &target.path, desired, target.max_bytes)
    }

    fn replace_exact_owned_file(
        path: &Path,
        stage: &Path,
        expected: &[u8],
        replacement: &[u8],
        max_bytes: usize,
    ) -> Result<(), DevelopmentError> {
        if read_regular_bounded(path, max_bytes)?.as_deref() != Some(expected) {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        prepare_update_stage(stage, replacement)?;
        if read_regular_bounded(path, max_bytes)?.as_deref() != Some(expected) {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        remove_exact_file(path, &sha256(expected), expected.len() as u64)?;
        publish_update_stage(stage, path, replacement, max_bytes)
    }

    fn prepare_update_stage(path: &Path, bytes: &[u8]) -> Result<(), DevelopmentError> {
        match fs::symlink_metadata(path) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Ok(_) => return Err(DevelopmentError::UpdateRecoveryRequired),
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        create_new_file(path, bytes, 0o600).map_err(|error| match error {
            DevelopmentError::PartialMutation => DevelopmentError::PartialMutation,
            _ => DevelopmentError::Filesystem,
        })
    }

    fn publish_update_stage(
        stage: &Path,
        target: &Path,
        expected: &[u8],
        max_bytes: usize,
    ) -> Result<(), DevelopmentError> {
        if read_regular_bounded(stage, max_bytes)?.as_deref() != Some(expected) {
            return Err(DevelopmentError::PartialMutation);
        }
        match fs::symlink_metadata(target) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Ok(_) => return Err(DevelopmentError::RegistrationConflict),
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        fs::hard_link(stage, target).map_err(|error| {
            if error.kind() == io::ErrorKind::AlreadyExists {
                DevelopmentError::RegistrationConflict
            } else {
                DevelopmentError::PartialMutation
            }
        })?;
        let stage_metadata =
            fs::symlink_metadata(stage).map_err(|_| DevelopmentError::PartialMutation)?;
        let target_metadata =
            fs::symlink_metadata(target).map_err(|_| DevelopmentError::PartialMutation)?;
        if stage_metadata.file_type().is_symlink()
            || !stage_metadata.is_file()
            || stage_metadata.nlink() != 2
            || target_metadata.file_type().is_symlink()
            || !target_metadata.is_file()
            || target_metadata.nlink() != 2
            || stage_metadata.dev() != target_metadata.dev()
            || stage_metadata.ino() != target_metadata.ino()
        {
            return Err(DevelopmentError::PartialMutation);
        }
        fs::remove_file(stage).map_err(|_| DevelopmentError::PartialMutation)?;
        sync_parent(stage).map_err(|_| DevelopmentError::PartialMutation)?;
        if read_regular_bounded(target, max_bytes)?.as_deref() != Some(expected) {
            return Err(DevelopmentError::PartialMutation);
        }
        Ok(())
    }

    fn exact_object<'a>(
        value: &'a Value,
        keys: &[&str],
    ) -> Result<&'a BTreeMap<String, Value>, DevelopmentError> {
        let fields = value.as_object().ok_or(DevelopmentError::StateInvalid)?;
        if fields.len() != keys.len() || keys.iter().any(|key| !fields.contains_key(*key)) {
            return Err(DevelopmentError::StateInvalid);
        }
        Ok(fields)
    }

    fn string<'a>(
        fields: &'a BTreeMap<String, Value>,
        key: &str,
    ) -> Result<&'a str, DevelopmentError> {
        fields
            .get(key)
            .and_then(Value::as_str)
            .ok_or(DevelopmentError::StateInvalid)
    }

    fn number(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, DevelopmentError> {
        fields
            .get(key)
            .and_then(Value::as_u64)
            .ok_or(DevelopmentError::StateInvalid)
    }

    fn string_array(
        fields: &BTreeMap<String, Value>,
        key: &str,
    ) -> Result<Vec<String>, DevelopmentError> {
        fields
            .get(key)
            .and_then(Value::as_array)
            .ok_or(DevelopmentError::StateInvalid)?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map(str::to_owned)
                    .ok_or(DevelopmentError::StateInvalid)
            })
            .collect()
    }

    fn valid_id(value: &str) -> bool {
        value.len() == 32
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    }

    fn valid_version(value: &str) -> bool {
        let parts = value.split('.').collect::<Vec<_>>();
        parts.len() == 3
            && parts.iter().all(|part| {
                !part.is_empty()
                    && part.bytes().all(|byte| byte.is_ascii_digit())
                    && (*part == "0" || !part.starts_with('0'))
            })
    }

    fn valid_sha256(value: &str) -> bool {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    }

    fn path_bytes(path: &Path) -> Result<Vec<u8>, DevelopmentError> {
        use std::os::unix::ffi::OsStrExt;
        let bytes = path.as_os_str().as_bytes();
        if bytes.is_empty() || bytes.len() > 4096 {
            return Err(DevelopmentError::StateInvalid);
        }
        Ok(bytes.to_vec())
    }

    fn file_identity(file: &File) -> Result<FileIdentity, DevelopmentError> {
        let metadata = file.metadata().map_err(|_| DevelopmentError::Filesystem)?;
        if !metadata.is_file() {
            return Err(DevelopmentError::SourceInvalid);
        }
        Ok(FileIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
            size: metadata.len(),
        })
    }

    fn open_nofollow_regular(path: &Path) -> Result<File, DevelopmentError> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(open_nofollow_flag())
            .open(path)
            .map_err(|_| DevelopmentError::InstallUnhealthy)?;
        let metadata = file
            .metadata()
            .map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if !metadata.is_file() || metadata.nlink() != 1 || metadata.uid() != effective_uid() {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        Ok(file)
    }

    #[cfg(target_os = "linux")]
    const fn open_nofollow_flag() -> i32 {
        0x20000
    }

    #[cfg(target_os = "macos")]
    const fn open_nofollow_flag() -> i32 {
        0x0100
    }

    fn effective_uid() -> u32 {
        unsafe extern "C" {
            fn geteuid() -> u32;
        }
        unsafe { geteuid() }
    }

    fn verify_regular_digest(
        path: &Path,
        expected_sha256: &str,
        expected_size: u64,
        require_executable: bool,
    ) -> Result<(), DevelopmentError> {
        verify_regular_digest_allowing_links(
            path,
            expected_sha256,
            expected_size,
            require_executable,
            1,
        )
    }

    fn verify_regular_digest_allowing_links(
        path: &Path,
        expected_sha256: &str,
        expected_size: u64,
        require_executable: bool,
        expected_links: u64,
    ) -> Result<(), DevelopmentError> {
        let metadata =
            fs::symlink_metadata(path).map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.len() != expected_size
            || metadata.nlink() != expected_links
            || metadata.uid() != effective_uid()
            || (require_executable && metadata.permissions().mode() & 0o111 == 0)
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(open_nofollow_flag())
            .open(path)
            .map_err(|_| DevelopmentError::InstallUnhealthy)?;
        let identity = file_identity(&file)?;
        let opened = file
            .metadata()
            .map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if identity.device != metadata.dev()
            || identity.inode != metadata.ino()
            || identity.size != metadata.len()
            || opened.nlink() != expected_links
            || opened.uid() != effective_uid()
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let (digest, size) = hash_reader(&mut file, MAX_BINARY_BYTES)?;
        if digest != expected_sha256 || size != expected_size {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        Ok(())
    }

    fn hash_reader<R: Read>(
        reader: &mut R,
        max_bytes: u64,
    ) -> Result<(String, u64), DevelopmentError> {
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        let mut total = 0_u64;
        loop {
            let count = reader
                .read(&mut buffer)
                .map_err(|_| DevelopmentError::Filesystem)?;
            if count == 0 {
                break;
            }
            total = total
                .checked_add(count as u64)
                .filter(|total| *total <= max_bytes)
                .ok_or(DevelopmentError::SourceInvalid)?;
            hasher.update(&buffer[..count]);
        }
        Ok((hex(&hasher.finalize()), total))
    }

    fn sha256(bytes: &[u8]) -> String {
        hex(&Sha256::digest(bytes))
    }

    fn hex(bytes: &[u8]) -> String {
        let mut output = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            use std::fmt::Write as _;
            let _ = write!(&mut output, "{byte:02x}");
        }
        output
    }

    fn read_regular_bounded(
        path: &Path,
        max_bytes: usize,
    ) -> Result<Option<Vec<u8>>, DevelopmentError> {
        let metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(DevelopmentError::Filesystem),
        };
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.len() > max_bytes as u64
        {
            return Err(DevelopmentError::RegistrationConflict);
        }
        let file = open_nofollow_regular(path)?;
        let opened = file.metadata().map_err(|_| DevelopmentError::Filesystem)?;
        if opened.dev() != metadata.dev()
            || opened.ino() != metadata.ino()
            || opened.len() != metadata.len()
        {
            return Err(DevelopmentError::RegistrationConflict);
        }
        let mut bytes = Vec::with_capacity(metadata.len() as usize);
        file.take((max_bytes + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| DevelopmentError::Filesystem)?;
        if bytes.len() > max_bytes {
            return Err(DevelopmentError::RegistrationConflict);
        }
        Ok(Some(bytes))
    }

    fn create_new_file(path: &Path, bytes: &[u8], mode: u32) -> Result<(), DevelopmentError> {
        let parent = path.parent().ok_or(DevelopmentError::Filesystem)?;
        ensure_directory_chain(parent, None)?;
        match fs::symlink_metadata(path) {
            Ok(_) => return Err(DevelopmentError::RegistrationConflict),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(path)
            .map_err(|error| {
                if error.kind() == io::ErrorKind::AlreadyExists {
                    DevelopmentError::RegistrationConflict
                } else {
                    DevelopmentError::Filesystem
                }
            })?;
        let write_result = (|| {
            file.write_all(bytes)
                .map_err(|_| DevelopmentError::Filesystem)?;
            file.sync_all().map_err(|_| DevelopmentError::Filesystem)?;
            file.set_permissions(fs::Permissions::from_mode(mode))
                .map_err(|_| DevelopmentError::Filesystem)?;
            let metadata = file.metadata().map_err(|_| DevelopmentError::Filesystem)?;
            if !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.uid() != effective_uid()
                || metadata.len() != bytes.len() as u64
            {
                return Err(DevelopmentError::Filesystem);
            }
            file.seek(SeekFrom::Start(0))
                .map_err(|_| DevelopmentError::Filesystem)?;
            let (digest, size) = hash_reader(&mut file, MAX_STATE_BYTES as u64)?;
            if digest != sha256(bytes) || size != bytes.len() as u64 {
                return Err(DevelopmentError::Filesystem);
            }
            sync_parent(path)
        })();
        if let Err(error) = write_result {
            return if remove_created_prefix(path, &mut file, bytes).is_ok() {
                Err(error)
            } else {
                Err(DevelopmentError::PartialMutation)
            };
        }
        Ok(())
    }

    fn remove_created_prefix(
        path: &Path,
        file: &mut File,
        expected: &[u8],
    ) -> Result<(), DevelopmentError> {
        let identity = file_identity(file)?;
        if identity.size > expected.len() as u64 {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        file.seek(SeekFrom::Start(0))
            .map_err(|_| DevelopmentError::Filesystem)?;
        let mut current_bytes = Vec::with_capacity(identity.size as usize);
        std::io::Read::by_ref(file)
            .take(expected.len() as u64 + 1)
            .read_to_end(&mut current_bytes)
            .map_err(|_| DevelopmentError::Filesystem)?;
        if current_bytes.len() as u64 != identity.size || !expected.starts_with(&current_bytes) {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let current = fs::symlink_metadata(path).map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if current.file_type().is_symlink()
            || !current.is_file()
            || current.dev() != identity.device
            || current.ino() != identity.inode
            || current.len() != identity.size
            || current.nlink() != 1
            || current.uid() != effective_uid()
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        fs::remove_file(path).map_err(|_| DevelopmentError::Filesystem)?;
        sync_parent(path)
    }

    fn remove_created_file_by_identity(path: &Path, file: &File) -> Result<(), DevelopmentError> {
        let identity = file_identity(file)?;
        let opened = file.metadata().map_err(|_| DevelopmentError::Filesystem)?;
        let current = fs::symlink_metadata(path).map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if opened.nlink() != 1
            || opened.uid() != effective_uid()
            || current.file_type().is_symlink()
            || !current.is_file()
            || current.dev() != identity.device
            || current.ino() != identity.inode
            || current.len() != identity.size
            || current.nlink() != 1
            || current.uid() != effective_uid()
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        fs::remove_file(path).map_err(|_| DevelopmentError::Filesystem)?;
        sync_parent(path)
    }

    fn remove_exact_file(
        path: &Path,
        expected_sha256: &str,
        expected_size: u64,
    ) -> Result<(), DevelopmentError> {
        let mut file = open_nofollow_regular(path)?;
        let identity = file_identity(&file)?;
        if identity.size != expected_size {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let (digest, size) = hash_reader(&mut file, MAX_BINARY_BYTES)?;
        if digest != expected_sha256 || size != expected_size {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let current = fs::symlink_metadata(path).map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if current.file_type().is_symlink()
            || !current.is_file()
            || current.dev() != identity.device
            || current.ino() != identity.inode
            || current.len() != identity.size
            || current.nlink() != 1
            || current.uid() != effective_uid()
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        fs::remove_file(path).map_err(|_| DevelopmentError::Filesystem)?;
        sync_parent(path)
    }

    fn rename_exact_file(
        source: &Path,
        destination: &Path,
        expected_sha256: &str,
        expected_size: u64,
        require_executable: bool,
    ) -> Result<(), DevelopmentError> {
        match fs::symlink_metadata(destination) {
            Ok(_) => return Err(DevelopmentError::RegistrationConflict),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(_) => return Err(DevelopmentError::Filesystem),
        }
        let source_metadata =
            fs::symlink_metadata(source).map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if source_metadata.file_type().is_symlink()
            || !source_metadata.is_file()
            || source_metadata.len() != expected_size
            || (require_executable && source_metadata.permissions().mode() & 0o111 == 0)
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let mut file = open_nofollow_regular(source)?;
        let identity = file_identity(&file)?;
        let (digest, size) = hash_reader(&mut file, MAX_BINARY_BYTES)?;
        if digest != expected_sha256 || size != expected_size {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        let current =
            fs::symlink_metadata(source).map_err(|_| DevelopmentError::InstallUnhealthy)?;
        if current.dev() != identity.device
            || current.ino() != identity.inode
            || current.len() != identity.size
            || current.nlink() != 1
            || current.uid() != effective_uid()
            || fs::symlink_metadata(destination).is_ok()
        {
            return Err(DevelopmentError::InstallUnhealthy);
        }
        fs::hard_link(source, destination).map_err(|error| {
            if error.kind() == io::ErrorKind::AlreadyExists {
                DevelopmentError::RegistrationConflict
            } else {
                DevelopmentError::Filesystem
            }
        })?;
        let moved =
            fs::symlink_metadata(destination).map_err(|_| DevelopmentError::PartialMutation)?;
        if moved.file_type().is_symlink()
            || !moved.is_file()
            || moved.dev() != identity.device
            || moved.ino() != identity.inode
            || moved.len() != identity.size
            || moved.nlink() != 2
        {
            return Err(DevelopmentError::PartialMutation);
        }
        fs::remove_file(source).map_err(|_| DevelopmentError::PartialMutation)?;
        let moved =
            fs::symlink_metadata(destination).map_err(|_| DevelopmentError::PartialMutation)?;
        if moved.dev() != identity.device
            || moved.ino() != identity.inode
            || moved.len() != identity.size
            || moved.nlink() != 1
        {
            return Err(DevelopmentError::PartialMutation);
        }
        sync_parent(source)?;
        if source.parent() != destination.parent() {
            sync_parent(destination)?;
        }
        Ok(())
    }

    fn ensure_private_root(path: &Path) -> Result<(), DevelopmentError> {
        if !path.is_absolute() {
            return Err(DevelopmentError::UserPathsUnavailable);
        }
        ensure_directory_chain(path, Some(path))?;
        let metadata = fs::symlink_metadata(path).map_err(|_| DevelopmentError::Filesystem)?;
        if metadata.uid() != effective_uid() || metadata.permissions().mode() & 0o077 != 0 {
            return Err(DevelopmentError::Filesystem);
        }
        Ok(())
    }

    fn ensure_directory_chain(
        path: &Path,
        private_from: Option<&Path>,
    ) -> Result<(), DevelopmentError> {
        use std::path::Component;

        if !path.is_absolute() {
            return Err(DevelopmentError::Filesystem);
        }
        let mut current = PathBuf::new();
        for component in path.components() {
            match component {
                Component::RootDir => current.push(Path::new("/")),
                Component::Normal(part) => current.push(part),
                _ => return Err(DevelopmentError::Filesystem),
            }
            let created = match fs::symlink_metadata(&current) {
                Ok(metadata) => {
                    if metadata.file_type().is_symlink() || !metadata.is_dir() {
                        return Err(DevelopmentError::Filesystem);
                    }
                    false
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    fs::create_dir(&current).map_err(|_| DevelopmentError::Filesystem)?;
                    let metadata =
                        fs::symlink_metadata(&current).map_err(|_| DevelopmentError::Filesystem)?;
                    if metadata.file_type().is_symlink() || !metadata.is_dir() {
                        return Err(DevelopmentError::Filesystem);
                    }
                    true
                }
                Err(_) => return Err(DevelopmentError::Filesystem),
            };
            let private = private_from.is_some_and(|root| current.starts_with(root));
            if created && private {
                fs::set_permissions(&current, fs::Permissions::from_mode(0o700))
                    .map_err(|_| DevelopmentError::Filesystem)?;
            }
            if private {
                let metadata =
                    fs::symlink_metadata(&current).map_err(|_| DevelopmentError::Filesystem)?;
                if metadata.uid() != effective_uid() || metadata.permissions().mode() & 0o077 != 0 {
                    return Err(DevelopmentError::Filesystem);
                }
            }
        }
        Ok(())
    }

    fn sync_parent(path: &Path) -> Result<(), DevelopmentError> {
        let parent = path.parent().ok_or(DevelopmentError::Filesystem)?;
        File::open(parent)
            .and_then(|file| file.sync_all())
            .map_err(|_| DevelopmentError::Filesystem)
    }

    fn random_id() -> Result<String, DevelopmentError> {
        let mut bytes = [0_u8; 16];
        getrandom::fill(&mut bytes).map_err(|_| DevelopmentError::EntropyUnavailable)?;
        Ok(hex(&bytes))
    }

    struct InstallLock {
        file: File,
    }

    impl InstallLock {
        fn acquire(path: &Path) -> Result<Self, DevelopmentError> {
            let create = OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(open_nofollow_flag())
                .open(path);
            let (mut file, newly_created) = match create {
                Ok(file) => (file, true),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                    let path_metadata =
                        fs::symlink_metadata(path).map_err(|_| DevelopmentError::InstallBusy)?;
                    if path_metadata.file_type().is_symlink()
                        || !path_metadata.is_file()
                        || path_metadata.nlink() != 1
                        || path_metadata.uid() != effective_uid()
                        || path_metadata.permissions().mode() & 0o077 != 0
                        || path_metadata.len() != LOCK_MAGIC.len() as u64
                    {
                        return Err(DevelopmentError::InstallBusy);
                    }
                    let mut file = OpenOptions::new()
                        .read(true)
                        .write(true)
                        .custom_flags(open_nofollow_flag())
                        .open(path)
                        .map_err(|_| DevelopmentError::InstallBusy)?;
                    let opened = file.metadata().map_err(|_| DevelopmentError::InstallBusy)?;
                    if opened.dev() != path_metadata.dev()
                        || opened.ino() != path_metadata.ino()
                        || opened.len() != path_metadata.len()
                    {
                        return Err(DevelopmentError::InstallBusy);
                    }
                    let mut existing = Vec::with_capacity(LOCK_MAGIC.len());
                    file.read_to_end(&mut existing)
                        .map_err(|_| DevelopmentError::InstallBusy)?;
                    if existing != LOCK_MAGIC {
                        return Err(DevelopmentError::InstallBusy);
                    }
                    file.seek(SeekFrom::Start(0))
                        .map_err(|_| DevelopmentError::InstallBusy)?;
                    (file, false)
                }
                Err(_) => return Err(DevelopmentError::Filesystem),
            };
            let metadata = file.metadata().map_err(|_| DevelopmentError::Filesystem)?;
            if !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.uid() != effective_uid()
                || (!newly_created && metadata.len() != LOCK_MAGIC.len() as u64)
            {
                return Err(DevelopmentError::InstallBusy);
            }
            if newly_created {
                file.set_permissions(fs::Permissions::from_mode(0o600))
                    .map_err(|_| DevelopmentError::Filesystem)?;
            }
            for attempt in 0..LOCK_ATTEMPTS {
                if try_flock_exclusive(&file)? {
                    let locked = file.metadata().map_err(|_| DevelopmentError::Filesystem)?;
                    let current =
                        fs::symlink_metadata(path).map_err(|_| DevelopmentError::InstallBusy)?;
                    if locked.dev() != metadata.dev()
                        || locked.ino() != metadata.ino()
                        || locked.nlink() != 1
                        || locked.uid() != effective_uid()
                        || current.file_type().is_symlink()
                        || !current.is_file()
                        || current.dev() != locked.dev()
                        || current.ino() != locked.ino()
                    {
                        return Err(DevelopmentError::InstallBusy);
                    }
                    if newly_created {
                        if file
                            .write_all(LOCK_MAGIC)
                            .and_then(|_| file.sync_all())
                            .is_err()
                        {
                            return if remove_created_prefix(path, &mut file, LOCK_MAGIC).is_ok() {
                                Err(DevelopmentError::Filesystem)
                            } else {
                                Err(DevelopmentError::PartialMutation)
                            };
                        }
                    }
                    return Ok(Self { file });
                }
                if attempt + 1 < LOCK_ATTEMPTS {
                    thread::sleep(LOCK_WAIT);
                }
            }
            drop(file);
            if newly_created {
                let _ = remove_exact_file(path, &sha256(&[]), 0);
            }
            Err(DevelopmentError::InstallBusy)
        }
    }

    impl Drop for InstallLock {
        fn drop(&mut self) {
            let _ = flock_unlock(&self.file);
        }
    }

    fn try_flock_exclusive(file: &File) -> Result<bool, DevelopmentError> {
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
            Err(DevelopmentError::Filesystem)
        }
    }

    fn flock_unlock(file: &File) -> Result<(), DevelopmentError> {
        use std::os::fd::AsRawFd;
        const LOCK_UN: i32 = 8;
        unsafe extern "C" {
            fn flock(fd: i32, operation: i32) -> i32;
        }
        if unsafe { flock(file.as_raw_fd(), LOCK_UN) } == 0 {
            Ok(())
        } else {
            Err(DevelopmentError::Filesystem)
        }
    }

    #[cfg(test)]
    mod tests {
        use std::collections::{BTreeMap, BTreeSet};
        use std::fs::{self, OpenOptions};
        use std::io::Write;
        use std::os::unix::fs::symlink;
        use std::path::{Path, PathBuf};

        use super::*;

        struct TestRoot(PathBuf);

        impl TestRoot {
            fn new(label: &str) -> Self {
                let path = std::env::temp_dir().join(format!(
                    "a0-browser-bridge-development-{label}-{}",
                    random_id().expect("test entropy")
                ));
                fs::create_dir(&path).expect("create isolated test root");
                Self(fs::canonicalize(path).expect("canonical isolated test root"))
            }

            fn path(&self) -> &Path {
                &self.0
            }
        }

        impl Drop for TestRoot {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }

        fn synthetic_paths(root: &Path) -> UserPaths {
            UserPaths {
                install_root: root.join("private-install"),
                home_root: Some(root.to_owned()),
                config_root: Some(root.join("config")),
            }
        }

        fn source_build(root: &Path, name: &str, bytes: &[u8]) -> SourceBuild {
            use std::os::unix::fs::PermissionsExt;

            let path = root.join(name);
            fs::write(&path, bytes).expect("write source fixture");
            fs::set_permissions(&path, fs::Permissions::from_mode(0o500))
                .expect("make source executable");
            let file = open_nofollow_regular(&path).expect("open source fixture");
            SourceBuild {
                identity: file_identity(&file).expect("source identity"),
                file,
                sha256: sha256(bytes),
            }
        }

        fn installed_fixture(
            root: &TestRoot,
            browsers: Vec<BrowserId>,
            version: &str,
            binary_bytes: &[u8],
        ) -> (UserPaths, DevelopmentState, PathBuf) {
            let paths = synthetic_paths(root.path());
            ensure_private_root(&paths.install_root).expect("private install root");
            let binary_digest = sha256(binary_bytes);
            let binary = binary_path(
                &paths.install_root,
                version,
                Platform::current(),
                architecture(),
                &binary_digest,
            );
            ensure_directory_chain(
                binary.parent().expect("binary parent"),
                Some(&paths.install_root),
            )
            .expect("private binary directories");
            create_new_file(&binary, binary_bytes, 0o500).expect("fixture binary");
            let manifest = generate_development_manifest(&binary).expect("fixture manifest");
            let registrations =
                registration_paths(&browsers, Platform::current(), &paths).expect("registrations");
            create_registrations(&registrations, manifest.as_bytes())
                .expect("fixture registrations");
            let registration_path_sha256 = registrations
                .keys()
                .map(|path| sha256(&path_bytes(path).expect("path bytes")))
                .collect();
            let state = DevelopmentState {
                install_id: "0123456789abcdef0123456789abcdef".to_owned(),
                companion_version: version.to_owned(),
                platform: Platform::current(),
                architecture: architecture().to_owned(),
                active_binary_sha256: binary_digest,
                active_binary_size: binary_bytes.len() as u64,
                registered_browsers: browsers,
                registration_path_sha256,
                manifest_sha256: sha256(manifest.as_bytes()),
            };
            create_new_file(
                &paths.install_root.join(STATE_FILE),
                state.encode().as_bytes(),
                0o600,
            )
            .expect("fixture state");
            verify_canonical_owned_state(&state, &paths).expect("healthy fixture install");
            (paths, state, binary)
        }

        #[test]
        fn foreign_regular_and_dangling_manifest_targets_survive_collision_checks() {
            for dangling in [false, true] {
                let root = TestRoot::new(if dangling { "dangling" } else { "foreign" });
                let target = root.path().join("manifest.json");
                if dangling {
                    symlink(root.path().join("missing-target"), &target)
                        .expect("create dangling manifest symlink");
                } else {
                    fs::write(&target, b"foreign manifest").expect("write foreign manifest");
                }
                let desired =
                    BTreeMap::from([(target.clone(), BTreeSet::from([BrowserId::Chrome]))]);

                assert_eq!(
                    validate_registration_collisions(None, &desired, &synthetic_paths(root.path())),
                    Err(DevelopmentError::RegistrationConflict)
                );
                let metadata = fs::symlink_metadata(&target).expect("foreign target retained");
                if dangling {
                    assert!(metadata.file_type().is_symlink());
                    assert!(!target.exists());
                } else {
                    assert_eq!(fs::read(&target).unwrap(), b"foreign manifest");
                }
            }
        }

        #[test]
        fn install_lock_refuses_symlink_without_truncating_target() {
            let root = TestRoot::new("lock-symlink");
            let target = root.path().join("foreign-target");
            let lock = root.path().join("development-install.lock");
            fs::write(&target, b"must survive").expect("write target");
            symlink(&target, &lock).expect("create lock symlink");

            assert!(matches!(
                InstallLock::acquire(&lock),
                Err(DevelopmentError::InstallBusy)
            ));
            assert_eq!(fs::read(&target).unwrap(), b"must survive");
            assert!(fs::symlink_metadata(&lock)
                .unwrap()
                .file_type()
                .is_symlink());

            let foreign_lock = root.path().join("foreign-lock");
            fs::write(&foreign_lock, b"tiny foreign file").expect("write foreign lock file");
            assert!(matches!(
                InstallLock::acquire(&foreign_lock),
                Err(DevelopmentError::InstallBusy)
            ));
            assert_eq!(fs::read(&foreign_lock).unwrap(), b"tiny foreign file");
        }

        #[test]
        fn rollback_never_removes_manifest_changed_after_creation() {
            let root = TestRoot::new("changed-rollback");
            let manifest = root.path().join("manifest.json");
            let original = b"owned manifest";
            let changed = b"foreign replacement";
            create_new_file(&manifest, original, 0o600).expect("create owned manifest");
            let mut writer = OpenOptions::new()
                .write(true)
                .truncate(true)
                .open(&manifest)
                .expect("simulate concurrent replacement");
            writer.write_all(changed).unwrap();
            writer.sync_all().unwrap();

            assert!(remove_created_registrations(
                &[manifest.clone()],
                &sha256(original),
                original.len() as u64,
            )
            .is_err());
            assert_eq!(fs::read(&manifest).unwrap(), changed);
        }

        #[test]
        fn owned_move_never_replaces_an_existing_destination() {
            use std::os::unix::fs::PermissionsExt;

            let root = TestRoot::new("move-collision");
            let source = root.path().join("owned-binary");
            let destination = root.path().join("foreign-destination");
            fs::write(&source, b"owned").unwrap();
            fs::set_permissions(&source, fs::Permissions::from_mode(0o500)).unwrap();
            fs::write(&destination, b"foreign").unwrap();

            assert_eq!(
                rename_exact_file(&source, &destination, &sha256(b"owned"), 5, true),
                Err(DevelopmentError::RegistrationConflict)
            );
            assert_eq!(fs::read(&source).unwrap(), b"owned");
            assert_eq!(fs::read(&destination).unwrap(), b"foreign");
        }

        #[test]
        fn state_parser_round_trips_exact_schema_and_rejects_extra_fields() {
            let state = DevelopmentState {
                install_id: "0123456789abcdef0123456789abcdef".to_owned(),
                companion_version: COMPANION_VERSION.to_owned(),
                platform: Platform::Linux,
                architecture: "aarch64".to_owned(),
                active_binary_sha256: "a".repeat(64),
                active_binary_size: 7,
                registered_browsers: vec![BrowserId::Chrome],
                registration_path_sha256: vec!["b".repeat(64)],
                manifest_sha256: "c".repeat(64),
            };
            let parsed = json::parse(state.encode().as_bytes()).unwrap();
            assert_eq!(parse_state(&parsed), Ok(state));

            let mut fields = parsed.as_object().unwrap().clone();
            fields.insert("path".to_owned(), Value::String("/private".to_owned()));
            assert_eq!(
                parse_state(&Value::Object(fields)),
                Err(DevelopmentError::StateInvalid)
            );
        }

        #[test]
        fn development_update_requires_an_existing_owned_install() {
            let root = TestRoot::new("update-not-installed");
            let source = source_build(root.path(), "candidate", b"new development binary");
            let paths = synthetic_paths(root.path());

            assert_eq!(
                update_from_source(&source, Platform::current(), &paths, UpdateFault::None),
                Err(DevelopmentError::UpdateNotInstalled)
            );
            assert!(!paths.install_root.exists());
        }

        #[test]
        fn development_update_is_idempotent_for_exact_current_source() {
            let root = TestRoot::new("update-current");
            let bytes = b"current development binary";
            let (paths, old_state, old_binary) =
                installed_fixture(&root, vec![BrowserId::Chrome], COMPANION_VERSION, bytes);
            let source = source_build(root.path(), "candidate", bytes);

            let result =
                update_from_source(&source, Platform::current(), &paths, UpdateFault::None)
                    .expect("exact update is idempotent");
            assert_eq!(result.action, DevelopmentAction::Update);
            assert_eq!(result.reason_code, "DEVELOPMENT_ALREADY_CURRENT");
            assert!(result.already_current);
            assert_eq!(
                load_state(&paths.install_root.join(STATE_FILE)).unwrap(),
                Some(old_state)
            );
            assert!(old_binary.exists());
            assert!(!paths.install_root.join(UPDATE_JOURNAL_FILE).exists());
        }

        #[test]
        fn development_update_switches_owned_targets_and_preserves_identity() {
            let root = TestRoot::new("update-success");
            let browsers = vec![BrowserId::Chrome, BrowserId::Edge];
            let (paths, old_state, old_binary) =
                installed_fixture(&root, browsers.clone(), "2.11.0", b"old binary");
            let source = source_build(root.path(), "candidate", b"new development binary");

            let result =
                update_from_source(&source, Platform::current(), &paths, UpdateFault::None)
                    .expect("owned update succeeds");
            assert_eq!(result.reason_code, "DEVELOPMENT_UPDATED");
            assert_eq!(result.registered_browsers, browsers);
            let new_state = load_state(&paths.install_root.join(STATE_FILE))
                .unwrap()
                .expect("updated state");
            assert_eq!(new_state.install_id, old_state.install_id);
            assert_eq!(new_state.registered_browsers, old_state.registered_browsers);
            assert_eq!(
                new_state.registration_path_sha256,
                old_state.registration_path_sha256
            );
            assert_eq!(new_state.active_binary_sha256, source.sha256);
            verify_canonical_owned_state(&new_state, &paths).expect("updated install healthy");
            assert!(old_binary.exists(), "old immutable binary is retained");
            assert!(!paths.install_root.join(UPDATE_JOURNAL_FILE).exists());
        }

        #[test]
        fn development_update_refuses_a_foreign_manifest_without_staging() {
            let root = TestRoot::new("update-foreign");
            let (paths, _, _) =
                installed_fixture(&root, vec![BrowserId::Chrome], "2.11.0", b"old binary");
            let registrations =
                registration_paths(&[BrowserId::Chrome], Platform::current(), &paths).unwrap();
            let target = registrations.keys().next().unwrap();
            fs::write(target, b"foreign manifest").expect("replace manifest with foreign bytes");
            let source = source_build(root.path(), "candidate", b"new development binary");
            let new_binary = binary_path(
                &paths.install_root,
                COMPANION_VERSION,
                Platform::current(),
                architecture(),
                &source.sha256,
            );

            assert_eq!(
                update_from_source(&source, Platform::current(), &paths, UpdateFault::None),
                Err(DevelopmentError::InstallUnhealthy)
            );
            assert_eq!(fs::read(target).unwrap(), b"foreign manifest");
            assert!(!new_binary.exists());
            assert!(!paths.install_root.join(UPDATE_JOURNAL_FILE).exists());
        }

        #[test]
        fn interrupted_update_recovers_only_known_bytes_then_completes() {
            let root = TestRoot::new("update-recovery");
            let browsers = vec![BrowserId::Chrome, BrowserId::Edge];
            let (paths, old_state, old_binary) =
                installed_fixture(&root, browsers, "2.11.0", b"old binary");
            let source = source_build(root.path(), "candidate", b"new development binary");

            assert_eq!(
                update_from_source(
                    &source,
                    Platform::current(),
                    &paths,
                    UpdateFault::AfterFirstManifest,
                ),
                Err(DevelopmentError::PartialMutation)
            );
            assert!(paths.install_root.join(UPDATE_JOURNAL_FILE).exists());

            let result =
                update_from_source(&source, Platform::current(), &paths, UpdateFault::None)
                    .expect("next update recovers and completes");
            assert_eq!(result.reason_code, "DEVELOPMENT_UPDATED");
            let new_state = load_state(&paths.install_root.join(STATE_FILE))
                .unwrap()
                .expect("recovered update state");
            assert_eq!(new_state.install_id, old_state.install_id);
            assert_eq!(new_state.active_binary_sha256, source.sha256);
            verify_canonical_owned_state(&new_state, &paths).expect("recovered install healthy");
            assert!(old_binary.exists());
            assert!(!paths.install_root.join(UPDATE_JOURNAL_FILE).exists());
        }

        #[test]
        fn interrupted_after_durable_journal_recovers_candidate_stage() {
            let root = TestRoot::new("update-journal-recovery");
            let (paths, old_state, old_binary) =
                installed_fixture(&root, vec![BrowserId::Chrome], "2.11.0", b"old binary");
            let source = source_build(root.path(), "candidate", b"new development binary");

            assert_eq!(
                update_from_source(
                    &source,
                    Platform::current(),
                    &paths,
                    UpdateFault::AfterJournal,
                ),
                Err(DevelopmentError::PartialMutation)
            );
            assert!(paths.install_root.join(UPDATE_JOURNAL_FILE).exists());
            let staged = binary_path(
                &paths.install_root,
                COMPANION_VERSION,
                Platform::current(),
                architecture(),
                &source.sha256,
            );
            assert!(
                !staged.exists(),
                "candidate is not published before recovery"
            );

            let result =
                update_from_source(&source, Platform::current(), &paths, UpdateFault::None)
                    .expect("next update rolls back the staged intent then completes");
            assert_eq!(result.reason_code, "DEVELOPMENT_UPDATED");
            let new_state = load_state(&paths.install_root.join(STATE_FILE))
                .unwrap()
                .expect("updated state");
            assert_eq!(new_state.install_id, old_state.install_id);
            assert_eq!(new_state.active_binary_sha256, source.sha256);
            verify_canonical_owned_state(&new_state, &paths).expect("recovered install healthy");
            assert!(old_binary.exists());
            assert!(!paths.install_root.join(UPDATE_JOURNAL_FILE).exists());
        }

        #[test]
        fn read_only_journal_inspection_preserves_exact_two_link_publication() {
            let root = TestRoot::new("update-journal-read-only");
            let (paths, old_state, _) =
                installed_fixture(&root, vec![BrowserId::Chrome], "2.11.0", b"old binary");
            let source = source_build(root.path(), "candidate", b"new development binary");
            let binary = binary_path(
                &paths.install_root,
                COMPANION_VERSION,
                Platform::current(),
                architecture(),
                &source.sha256,
            );
            let manifest = generate_development_manifest(&binary).unwrap();
            let journal = DevelopmentUpdateJournal {
                transaction_id: "a".repeat(32),
                new_state: DevelopmentState {
                    install_id: old_state.install_id.clone(),
                    companion_version: COMPANION_VERSION.to_owned(),
                    platform: Platform::current(),
                    architecture: architecture().to_owned(),
                    active_binary_sha256: source.sha256,
                    active_binary_size: source.identity.size,
                    registered_browsers: old_state.registered_browsers.clone(),
                    registration_path_sha256: old_state.registration_path_sha256.clone(),
                    manifest_sha256: sha256(manifest.as_bytes()),
                },
                old_state,
            };
            let journal_path = paths.install_root.join(UPDATE_JOURNAL_FILE);
            let stage = journal_publication_stage_path(&journal_path, &journal.transaction_id)
                .expect("journal stage path");
            let bytes = journal.encode().unwrap();
            create_new_file(&stage, bytes.as_bytes(), 0o600).unwrap();
            fs::hard_link(&stage, &journal_path).expect("simulate linked publication");

            assert!(load_update_journal(&journal_path, false).unwrap().is_some());
            assert!(
                stage.exists(),
                "read-only inspection must not mutate the stage"
            );
            assert_eq!(fs::symlink_metadata(&stage).unwrap().nlink(), 2);
            assert_eq!(fs::symlink_metadata(&journal_path).unwrap().nlink(), 2);

            assert!(load_update_journal(&journal_path, true).unwrap().is_some());
            assert!(!stage.exists(), "locked recovery may finish publication");
            assert_eq!(fs::symlink_metadata(&journal_path).unwrap().nlink(), 1);
        }

        #[test]
        fn recovery_finishes_exact_two_link_publication_without_partial_target() {
            let root = TestRoot::new("update-linked-stage");
            let target_path = root.path().join("manifest.json");
            let stage_path = root.path().join(".journal-stage.tmp");
            let old = b"old complete manifest".to_vec();
            let new = b"new complete manifest".to_vec();
            create_new_file(&target_path, &old, 0o600).unwrap();
            create_new_file(&stage_path, &new, 0o600).unwrap();
            remove_exact_file(&target_path, &sha256(&old), old.len() as u64).unwrap();
            fs::hard_link(&stage_path, &target_path).expect("simulate crash after publication");
            let target = UpdateTarget {
                path: target_path.clone(),
                stage: stage_path.clone(),
                old,
                new: new.clone(),
                max_bytes: MAX_MANIFEST_BYTES,
            };

            preflight_update_target(&target).expect("two-link state is journal-owned");
            cleanup_update_stage(&target).expect("finish exact publication");
            assert!(!stage_path.exists());
            assert_eq!(fs::read(&target_path).unwrap(), new);
            assert_eq!(fs::symlink_metadata(&target_path).unwrap().nlink(), 1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn development_cli_requires_explicit_confirmation_and_exact_options() {
        let parsed = parse_options(&[
            "install".into(),
            "--browser".into(),
            "chrome".into(),
            "--yes".into(),
            "--json".into(),
        ])
        .expect("valid development options");
        assert_eq!(parsed.action, DevelopmentAction::Install);
        assert_eq!(parsed.browsers, vec![BrowserId::Chrome]);
        assert!(parsed.confirmed);
        assert!(parsed.json);
        let update = parse_options(&["update".into(), "--yes".into(), "--json".into()])
            .expect("valid update options");
        assert_eq!(update.action, DevelopmentAction::Update);
        assert!(update.confirmed);
        assert!(parse_options(&["update".into(), "--browser".into(), "chrome".into(),]).is_err());
        assert!(parse_options(&["status".into(), "--yes".into()]).is_err());
        assert!(parse_options(&[
            "install".into(),
            "--browser".into(),
            "auto".into(),
            "--browser".into(),
            "chrome".into(),
        ])
        .is_err());
    }

    #[test]
    fn result_projection_is_exact_and_redacted() {
        let result = DevelopmentResult {
            action: DevelopmentAction::Status,
            state: STATE_NOT_INSTALLED,
            reason_code: "DEVELOPMENT_NOT_INSTALLED",
            registered_browsers: vec![BrowserId::Chrome],
            already_current: false,
            mutation_allowed: false,
            exit_code: crate::EXIT_NOT_INSTALLED,
        };
        let encoded = result.to_json();
        let parsed = json::parse(encoded.as_bytes()).unwrap();
        let fields = parsed.as_object().unwrap();
        assert_eq!(fields.len(), 14);
        assert_eq!(
            fields.get("channel").and_then(crate::json::Value::as_str),
            Some(DEVELOPMENT_CHANNEL)
        );
        assert!(!encoded.contains("/Users/"));
        assert!(!encoded.contains("http://"));
        assert!(!encoded.contains("private"));
    }
}
