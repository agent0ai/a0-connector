use std::ffi::OsString;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Platform {
    Macos,
    Windows,
    Linux,
    Unsupported,
}

impl Platform {
    pub fn current() -> Self {
        if cfg!(target_os = "macos") {
            Self::Macos
        } else if cfg!(target_os = "windows") {
            Self::Windows
        } else if cfg!(target_os = "linux") {
            Self::Linux
        } else {
            Self::Unsupported
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Macos => "macos",
            Self::Windows => "windows",
            Self::Linux => "linux",
            Self::Unsupported => "unsupported",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UserPaths {
    pub install_root: PathBuf,
    pub home_root: Option<PathBuf>,
    pub config_root: Option<PathBuf>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PathDiscoveryError {
    UnsupportedPlatform,
    RequiredEnvironmentMissing,
    RootNotAbsolute,
}

impl PathDiscoveryError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::UnsupportedPlatform => "UNSUPPORTED_PLATFORM",
            Self::RequiredEnvironmentMissing => "USER_ROOT_UNAVAILABLE",
            Self::RootNotAbsolute => "USER_ROOT_NOT_ABSOLUTE",
        }
    }
}

pub fn discover_user_paths() -> Result<UserPaths, PathDiscoveryError> {
    discover_user_paths_with(Platform::current(), |name| std::env::var_os(name))
}

pub fn discover_user_paths_with<F>(
    platform: Platform,
    lookup: F,
) -> Result<UserPaths, PathDiscoveryError>
where
    F: Fn(&str) -> Option<OsString>,
{
    match platform {
        Platform::Macos => {
            let home = required_absolute("HOME", &lookup, false)?;
            Ok(UserPaths {
                install_root: home
                    .join("Library")
                    .join("Application Support")
                    .join("Agent Zero")
                    .join(install_directory_name()),
                home_root: Some(home),
                config_root: None,
            })
        }
        Platform::Windows => {
            let local_app_data = required_absolute("LOCALAPPDATA", &lookup, true)?;
            Ok(UserPaths {
                install_root: local_app_data.join("Agent Zero").join("Browser Bridge"),
                home_root: None,
                config_root: None,
            })
        }
        Platform::Linux => {
            let home = required_absolute("HOME", &lookup, false)?;
            let install_root = optional_absolute("XDG_DATA_HOME", &lookup, false)?
                .unwrap_or_else(|| home.join(".local").join("share"))
                .join("agent-zero")
                .join(install_directory_name());
            let config_root = optional_absolute("XDG_CONFIG_HOME", &lookup, false)?
                .unwrap_or_else(|| home.join(".config"));
            Ok(UserPaths {
                install_root,
                home_root: Some(home),
                config_root: Some(config_root),
            })
        }
        Platform::Unsupported => Err(PathDiscoveryError::UnsupportedPlatform),
    }
}

#[cfg(not(feature = "local-development"))]
const fn install_directory_name() -> &'static str {
    if cfg!(target_os = "macos") {
        "Browser Bridge"
    } else {
        "browser-bridge"
    }
}

#[cfg(feature = "local-development")]
const fn install_directory_name() -> &'static str {
    if cfg!(target_os = "macos") {
        "Browser Bridge Development"
    } else {
        "browser-bridge-development"
    }
}

fn required_absolute<F>(
    name: &str,
    lookup: &F,
    windows: bool,
) -> Result<PathBuf, PathDiscoveryError>
where
    F: Fn(&str) -> Option<OsString>,
{
    optional_absolute(name, lookup, windows)?.ok_or(PathDiscoveryError::RequiredEnvironmentMissing)
}

fn optional_absolute<F>(
    name: &str,
    lookup: &F,
    windows: bool,
) -> Result<Option<PathBuf>, PathDiscoveryError>
where
    F: Fn(&str) -> Option<OsString>,
{
    let Some(raw) = lookup(name) else {
        return Ok(None);
    };
    if raw.is_empty() {
        return Ok(None);
    }
    let path = PathBuf::from(raw);
    let absolute = if windows {
        is_windows_absolute(&path)
    } else {
        path.is_absolute()
    };
    if !absolute {
        return Err(PathDiscoveryError::RootNotAbsolute);
    }
    Ok(Some(path))
}

fn is_windows_absolute(path: &Path) -> bool {
    let Some(value) = path.to_str() else {
        return false;
    };
    let bytes = value.as_bytes();
    (bytes.len() >= 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/'))
        || value.starts_with("\\\\")
}

pub const fn architecture() -> &'static str {
    if cfg!(target_arch = "x86_64") {
        "x86_64"
    } else if cfg!(target_arch = "aarch64") {
        "aarch64"
    } else {
        "unsupported"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linux_xdg_paths_must_be_absolute() {
        let result = discover_user_paths_with(Platform::Linux, |name| match name {
            "HOME" => Some(OsString::from("/home/test")),
            "XDG_DATA_HOME" => Some(OsString::from("relative")),
            _ => None,
        });
        assert_eq!(result, Err(PathDiscoveryError::RootNotAbsolute));
    }

    #[test]
    fn windows_install_is_per_user() {
        let paths = discover_user_paths_with(Platform::Windows, |name| {
            (name == "LOCALAPPDATA").then(|| OsString::from(r"C:\Users\fixture\AppData\Local"))
        })
        .expect("fixture path should resolve");
        assert!(paths.install_root.ends_with("Agent Zero/Browser Bridge"));
    }
}
