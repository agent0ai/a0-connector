use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::platform::{Platform, UserPaths};
use crate::NATIVE_HOST_NAME;

pub const BROWSER_REGISTRY_CONTRACT: &str = "a0.browser-bridge.browser-registry.v1";
pub const BROWSER_REGISTRY_SOURCE: &str = include_str!("../browser-registry-v1.json");

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum BrowserId {
    Chrome,
    Edge,
    Brave,
    Vivaldi,
    Opera,
    Chromium,
}

impl BrowserId {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Chrome => "chrome",
            Self::Edge => "edge",
            Self::Brave => "brave",
            Self::Vivaldi => "vivaldi",
            Self::Opera => "opera",
            Self::Chromium => "chromium",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "chrome" => Some(Self::Chrome),
            "edge" => Some(Self::Edge),
            "brave" => Some(Self::Brave),
            "vivaldi" => Some(Self::Vivaldi),
            "opera" => Some(Self::Opera),
            "chromium" => Some(Self::Chromium),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct BrowserRegistration {
    pub id: BrowserId,
    pub display_name: &'static str,
    pub macos_manifest_suffix: &'static str,
    pub linux_manifest_suffix: &'static str,
    pub linux_compatibility_suffix: Option<&'static str>,
    pub windows_registry_key: &'static str,
    macos_application: &'static str,
    macos_executable: &'static str,
    linux_launchers: &'static [&'static str],
}

#[cfg(not(feature = "local-development"))]
const CHROME_WINDOWS_KEY: &str =
    r"HKCU\Software\Google\Chrome\NativeMessagingHosts\io.agentzero.browser_bridge";
#[cfg(feature = "local-development")]
const CHROME_WINDOWS_KEY: &str =
    r"HKCU\Software\Google\Chrome\NativeMessagingHosts\io.agentzero.browser_bridge.dev";

#[cfg(not(feature = "local-development"))]
const EDGE_WINDOWS_KEY: &str =
    r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts\io.agentzero.browser_bridge";
#[cfg(feature = "local-development")]
const EDGE_WINDOWS_KEY: &str =
    r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts\io.agentzero.browser_bridge.dev";

#[cfg(not(feature = "local-development"))]
const CHROMIUM_WINDOWS_KEY: &str =
    r"HKCU\Software\Chromium\NativeMessagingHosts\io.agentzero.browser_bridge";
#[cfg(feature = "local-development")]
const CHROMIUM_WINDOWS_KEY: &str =
    r"HKCU\Software\Chromium\NativeMessagingHosts\io.agentzero.browser_bridge.dev";

pub const BROWSERS: &[BrowserRegistration] = &[
    BrowserRegistration {
        id: BrowserId::Chrome,
        display_name: "Google Chrome",
        macos_manifest_suffix: "Library/Application Support/Google/Chrome/NativeMessagingHosts",
        linux_manifest_suffix: "google-chrome/NativeMessagingHosts",
        linux_compatibility_suffix: None,
        windows_registry_key: CHROME_WINDOWS_KEY,
        macos_application: "Google Chrome.app",
        macos_executable: "Google Chrome",
        linux_launchers: &["google-chrome-stable"],
    },
    BrowserRegistration {
        id: BrowserId::Edge,
        display_name: "Microsoft Edge",
        macos_manifest_suffix: "Library/Application Support/Microsoft Edge/NativeMessagingHosts",
        linux_manifest_suffix: "microsoft-edge/NativeMessagingHosts",
        linux_compatibility_suffix: None,
        windows_registry_key: EDGE_WINDOWS_KEY,
        macos_application: "Microsoft Edge.app",
        macos_executable: "Microsoft Edge",
        linux_launchers: &["microsoft-edge"],
    },
    BrowserRegistration {
        id: BrowserId::Brave,
        display_name: "Brave",
        macos_manifest_suffix:
            "Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts",
        linux_manifest_suffix: "BraveSoftware/Brave-Browser/NativeMessagingHosts",
        linux_compatibility_suffix: None,
        windows_registry_key: CHROME_WINDOWS_KEY,
        macos_application: "Brave Browser.app",
        macos_executable: "Brave Browser",
        linux_launchers: &["brave-browser-stable"],
    },
    BrowserRegistration {
        id: BrowserId::Vivaldi,
        display_name: "Vivaldi",
        macos_manifest_suffix: "Library/Application Support/Vivaldi/NativeMessagingHosts",
        linux_manifest_suffix: "vivaldi/NativeMessagingHosts",
        linux_compatibility_suffix: None,
        windows_registry_key: CHROME_WINDOWS_KEY,
        macos_application: "Vivaldi.app",
        macos_executable: "Vivaldi",
        linux_launchers: &["vivaldi-stable"],
    },
    BrowserRegistration {
        id: BrowserId::Opera,
        display_name: "Opera",
        macos_manifest_suffix:
            "Library/Application Support/com.operasoftware.Opera/NativeMessagingHosts",
        linux_manifest_suffix: "opera/NativeMessagingHosts",
        linux_compatibility_suffix: Some("google-chrome/NativeMessagingHosts"),
        windows_registry_key: CHROME_WINDOWS_KEY,
        macos_application: "Opera.app",
        macos_executable: "Opera",
        linux_launchers: &["opera"],
    },
    BrowserRegistration {
        id: BrowserId::Chromium,
        display_name: "Chromium",
        macos_manifest_suffix: "Library/Application Support/Chromium/NativeMessagingHosts",
        linux_manifest_suffix: "chromium/NativeMessagingHosts",
        linux_compatibility_suffix: None,
        windows_registry_key: CHROMIUM_WINDOWS_KEY,
        macos_application: "Chromium.app",
        macos_executable: "Chromium",
        linux_launchers: &["chromium"],
    },
];

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RegistrationLocation {
    ManifestFiles(Vec<PathBuf>),
    WindowsCurrentUser {
        registry_key: &'static str,
        manifest_path: PathBuf,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum BrowserDiscoveryError {
    UnsupportedPlatform,
    RequiredPathUnavailable,
    Filesystem,
}

pub fn browser(browser: BrowserId) -> &'static BrowserRegistration {
    BROWSERS
        .iter()
        .find(|entry| entry.id == browser)
        .expect("compiled browser registry must be complete")
}

pub fn registration_location(
    browser_id: BrowserId,
    platform: Platform,
    paths: &UserPaths,
) -> Option<RegistrationLocation> {
    let entry = browser(browser_id);
    match platform {
        Platform::Macos => {
            let home = paths.home_root.as_ref()?;
            Some(RegistrationLocation::ManifestFiles(vec![append_suffix(
                home,
                entry.macos_manifest_suffix,
            )
            .join(format!("{NATIVE_HOST_NAME}.json"))]))
        }
        Platform::Linux => {
            let config = paths.config_root.as_ref()?;
            let mut files = vec![append_suffix(config, entry.linux_manifest_suffix)
                .join(format!("{NATIVE_HOST_NAME}.json"))];
            if let Some(suffix) = entry.linux_compatibility_suffix {
                files.push(append_suffix(config, suffix).join(format!("{NATIVE_HOST_NAME}.json")));
            }
            Some(RegistrationLocation::ManifestFiles(files))
        }
        Platform::Windows => Some(RegistrationLocation::WindowsCurrentUser {
            registry_key: entry.windows_registry_key,
            manifest_path: paths
                .install_root
                .join("manifests")
                .join(entry.id.as_str())
                .join(format!("{NATIVE_HOST_NAME}.json")),
        }),
        Platform::Unsupported => None,
    }
}

/// Discover stable browsers from exact compiled conventional locations only.
///
/// This never invokes a browser, searches `PATH`, walks profiles, or treats a
/// Snap/Flatpak export as a native-package installation.
pub(crate) fn discover_stable_browsers(
    platform: Platform,
    paths: &UserPaths,
) -> Result<Vec<BrowserId>, BrowserDiscoveryError> {
    discover_stable_browsers_with(
        platform,
        paths,
        Path::new("/Applications"),
        &[PathBuf::from("/usr/bin")],
    )
}

fn discover_stable_browsers_with(
    platform: Platform,
    paths: &UserPaths,
    macos_system_applications: &Path,
    linux_binary_directories: &[PathBuf],
) -> Result<Vec<BrowserId>, BrowserDiscoveryError> {
    let mut discovered = std::collections::BTreeSet::new();
    match platform {
        Platform::Macos => {
            let home = paths
                .home_root
                .as_ref()
                .filter(|path| path.is_absolute())
                .ok_or(BrowserDiscoveryError::RequiredPathUnavailable)?;
            if !macos_system_applications.is_absolute() {
                return Err(BrowserDiscoveryError::RequiredPathUnavailable);
            }
            let application_roots = [
                macos_system_applications.to_path_buf(),
                home.join("Applications"),
            ];
            for entry in BROWSERS {
                for root in &application_roots {
                    let executable = root
                        .join(entry.macos_application)
                        .join("Contents")
                        .join("MacOS")
                        .join(entry.macos_executable);
                    if executable_file_at(&executable, home)? {
                        discovered.insert(entry.id);
                        break;
                    }
                }
            }
        }
        Platform::Linux => {
            let home = paths
                .home_root
                .as_ref()
                .filter(|path| path.is_absolute())
                .ok_or(BrowserDiscoveryError::RequiredPathUnavailable)?;
            if linux_binary_directories.is_empty()
                || linux_binary_directories
                    .iter()
                    .any(|path| !path.is_absolute())
            {
                return Err(BrowserDiscoveryError::RequiredPathUnavailable);
            }
            for entry in BROWSERS {
                'launchers: for root in linux_binary_directories {
                    for launcher in entry.linux_launchers {
                        if executable_file_at(&root.join(launcher), home)? {
                            discovered.insert(entry.id);
                            break 'launchers;
                        }
                    }
                }
            }
        }
        Platform::Windows | Platform::Unsupported => {
            return Err(BrowserDiscoveryError::UnsupportedPlatform)
        }
    }
    Ok(discovered.into_iter().collect())
}

fn executable_file_at(path: &Path, home: &Path) -> Result<bool, BrowserDiscoveryError> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(_) => return Err(BrowserDiscoveryError::Filesystem),
    };
    if !metadata.is_file() || !is_executable(&metadata) {
        return Ok(false);
    }
    let resolved = fs::canonicalize(path).map_err(|_| BrowserDiscoveryError::Filesystem)?;
    if is_sandboxed_browser_path(&resolved, home) {
        return Ok(false);
    }
    Ok(true)
}

fn is_sandboxed_browser_path(path: &Path, home: &Path) -> bool {
    path.starts_with("/snap")
        || path.starts_with("/var/lib/snapd/snap")
        || path.starts_with("/var/lib/flatpak")
        || path.starts_with(home.join(".local/share/flatpak"))
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

fn append_suffix(root: &std::path::Path, suffix: &str) -> PathBuf {
    suffix
        .split('/')
        .fold(root.to_path_buf(), |path, part| path.join(part))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct FixtureRoot(PathBuf);

    impl FixtureRoot {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "a0-browser-discovery-{}-{}",
                std::process::id(),
                FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            Self(path)
        }

        fn paths(&self, platform: Platform) -> UserPaths {
            UserPaths {
                install_root: self.0.join("install"),
                home_root: Some(self.0.join("home")),
                config_root: (platform == Platform::Linux).then(|| self.0.join("config")),
            }
        }
    }

    impl Drop for FixtureRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(unix)]
    fn create_executable(path: &Path) {
        use std::os::unix::fs::PermissionsExt;

        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, b"fixture browser executable").unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o500)).unwrap();
    }

    #[test]
    fn registry_has_exact_supported_family_set() {
        let ids = BROWSERS
            .iter()
            .map(|entry| entry.id.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            ids,
            ["chrome", "edge", "brave", "vivaldi", "opera", "chromium"]
        );
        assert!(BROWSERS
            .iter()
            .all(|entry| entry.windows_registry_key.starts_with("HKCU\\")));
    }

    #[test]
    fn opera_linux_has_vendor_and_compatibility_manifests() {
        let paths = UserPaths {
            install_root: PathBuf::from("/home/test/.local/share/agent-zero/browser-bridge"),
            home_root: Some(PathBuf::from("/home/test")),
            config_root: Some(PathBuf::from("/home/test/.config")),
        };
        let Some(RegistrationLocation::ManifestFiles(files)) =
            registration_location(BrowserId::Opera, Platform::Linux, &paths)
        else {
            panic!("expected Linux manifest paths")
        };
        assert_eq!(files.len(), 2);
    }

    #[cfg(unix)]
    #[test]
    fn browser_discovery_macos_uses_exact_system_and_user_stable_apps() {
        let fixture = FixtureRoot::new();
        let paths = fixture.paths(Platform::Macos);
        let system_applications = fixture.0.join("Applications");
        let user_applications = paths.home_root.as_ref().unwrap().join("Applications");
        create_executable(
            &system_applications.join("Google Chrome.app/Contents/MacOS/Google Chrome"),
        );
        create_executable(
            &user_applications.join("Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        );
        create_executable(
            &system_applications.join("Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
        );

        assert_eq!(
            discover_stable_browsers_with(Platform::Macos, &paths, &system_applications, &[])
                .unwrap(),
            vec![BrowserId::Chrome, BrowserId::Edge]
        );
    }

    #[cfg(unix)]
    #[test]
    fn browser_discovery_linux_uses_only_compiled_stable_launcher_names() {
        let fixture = FixtureRoot::new();
        let paths = fixture.paths(Platform::Linux);
        let system_bin = fixture.0.join("usr/bin");
        let arbitrary_bin = fixture.0.join("arbitrary/path/bin");
        create_executable(&system_bin.join("google-chrome-stable"));
        create_executable(&system_bin.join("microsoft-edge-beta"));
        create_executable(&arbitrary_bin.join("vivaldi-stable"));

        assert_eq!(
            discover_stable_browsers_with(
                Platform::Linux,
                &paths,
                Path::new("/Applications"),
                std::slice::from_ref(&system_bin),
            )
            .unwrap(),
            vec![BrowserId::Chrome]
        );
        assert!(is_sandboxed_browser_path(
            Path::new("/snap/bin/chromium"),
            &fixture.0
        ));
        assert!(is_sandboxed_browser_path(
            &paths
                .home_root
                .as_ref()
                .unwrap()
                .join(".local/share/flatpak/exports/bin/com.brave.Browser"),
            paths.home_root.as_ref().unwrap(),
        ));
    }
}
