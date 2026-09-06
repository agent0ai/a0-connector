use std::collections::BTreeSet;

use crate::json;
use crate::native_host::framing_self_test;
use crate::platform::{architecture, discover_user_paths, Platform};
use crate::registry::{BROWSERS, BROWSER_REGISTRY_CONTRACT, BROWSER_REGISTRY_SOURCE};
use crate::release::release_trust_configured;
use crate::{COMPANION_VERSION, SELF_TEST_CONTRACT, STATUS_CONTRACT};

#[derive(Clone, Debug)]
pub struct LocalStatus {
    pub state: &'static str,
    pub reason_code: &'static str,
    pub platform: &'static str,
    pub architecture: &'static str,
    pub install_root: &'static str,
    pub release_trust: &'static str,
    pub native_host: &'static str,
    pub registered_browser_count: usize,
}

impl LocalStatus {
    pub fn collect() -> Self {
        let platform = Platform::current();
        let trust_configured = release_trust_configured();
        match discover_user_paths() {
            Ok(paths) => {
                let (state, reason_code, registered_browser_count) =
                    match crate::install::verify_installed_status(&paths) {
                        Ok(Some(status)) => (
                            "installed",
                            "INSTALL_VERIFIED",
                            status.registered_browser_count,
                        ),
                        Ok(None) => ("not_installed", "INSTALL_STATE_MISSING", 0),
                        Err(error) => ("blocked", error.reason_code(), 0),
                    };
                Self {
                    state,
                    reason_code,
                    platform: platform.as_str(),
                    architecture: architecture(),
                    install_root: "resolved",
                    release_trust: if trust_configured {
                        "configured"
                    } else {
                        "not_configured"
                    },
                    native_host: if trust_configured {
                        "enabled"
                    } else {
                        "disabled"
                    },
                    registered_browser_count,
                }
            }
            Err(error) => Self {
                state: "blocked",
                reason_code: error.reason_code(),
                platform: platform.as_str(),
                architecture: architecture(),
                install_root: "unavailable",
                release_trust: if trust_configured {
                    "configured"
                } else {
                    "not_configured"
                },
                native_host: "disabled",
                registered_browser_count: 0,
            },
        }
    }

    pub fn to_json(&self) -> String {
        json::object(&[
            ("contract", json::quote(STATUS_CONTRACT)),
            ("schema_version", "1".to_owned()),
            ("companion_version", json::quote(COMPANION_VERSION)),
            ("state", json::quote(self.state)),
            ("reason_code", json::quote(self.reason_code)),
            ("platform", json::quote(self.platform)),
            ("architecture", json::quote(self.architecture)),
            ("install_root", json::quote(self.install_root)),
            ("release_trust", json::quote(self.release_trust)),
            ("native_host", json::quote(self.native_host)),
            (
                "registered_browser_count",
                self.registered_browser_count.to_string(),
            ),
        ])
    }

    pub fn exit_code(&self) -> u8 {
        match self.state {
            "not_installed" => crate::EXIT_NOT_INSTALLED,
            "blocked" | "unknown" => crate::EXIT_INTEGRITY_OR_POLICY,
            _ => crate::EXIT_OK,
        }
    }
}

#[derive(Clone, Debug)]
pub struct SelfTestReport {
    pub state: &'static str,
    pub framing: &'static str,
    pub registry: &'static str,
    pub path_discovery: &'static str,
    pub install_ready: bool,
    pub reason_code: &'static str,
}

impl SelfTestReport {
    pub fn run() -> Self {
        let framing_ok = framing_self_test();
        let registry_ok = registry_self_test();
        let paths_ok = discover_user_paths().is_ok();
        let core_ok = framing_ok && registry_ok;
        let trust_ready = release_trust_configured();
        Self {
            state: if core_ok { "passed" } else { "failed" },
            framing: if framing_ok { "passed" } else { "failed" },
            registry: if registry_ok { "passed" } else { "failed" },
            path_discovery: if paths_ok { "passed" } else { "unavailable" },
            install_ready: core_ok && paths_ok && trust_ready,
            reason_code: if !core_ok {
                "SELF_TEST_INVARIANT_FAILED"
            } else if !trust_ready {
                "RELEASE_TRUST_NOT_CONFIGURED"
            } else if !paths_ok {
                "USER_ROOT_UNAVAILABLE"
            } else {
                "SELF_TEST_PASSED"
            },
        }
    }

    pub fn to_json(&self) -> String {
        json::object(&[
            ("contract", json::quote(SELF_TEST_CONTRACT)),
            ("schema_version", "1".to_owned()),
            ("companion_version", json::quote(COMPANION_VERSION)),
            ("state", json::quote(self.state)),
            ("framing", json::quote(self.framing)),
            ("registry", json::quote(self.registry)),
            ("path_discovery", json::quote(self.path_discovery)),
            ("install_ready", self.install_ready.to_string()),
            ("reason_code", json::quote(self.reason_code)),
        ])
    }
}

fn registry_self_test() -> bool {
    let ids = BROWSERS
        .iter()
        .map(|entry| entry.id.as_str())
        .collect::<BTreeSet<_>>();
    ids.len() == 6
        && BROWSER_REGISTRY_SOURCE.contains(BROWSER_REGISTRY_CONTRACT)
        && BROWSERS
            .iter()
            .all(|entry| entry.windows_registry_key.starts_with("HKCU\\"))
}

pub fn redacted_human_status(status: &LocalStatus) -> String {
    format!(
        "Browser Bridge: {} ({})\nPlatform: {}/{}\nInstall root: {}\nRelease trust: {}\nNative host: {}",
        status.state,
        status.reason_code,
        status.platform,
        status.architecture,
        status.install_root,
        status.release_trust,
        status.native_host,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_json_contains_no_home_path() {
        let status = LocalStatus::collect();
        let serialized = status.to_json();
        if let Some(home) = std::env::var_os("HOME") {
            let home = home.to_string_lossy();
            assert!(!serialized.contains(home.as_ref()));
        }
        assert!(!serialized.contains("http://"));
        assert!(!serialized.contains("https://"));
    }

    #[test]
    fn self_test_reports_compiled_trust_and_local_checks_not_installation() {
        let report = SelfTestReport::run();
        assert!(release_trust_configured());
        assert_eq!(report.framing, "passed");
        assert_eq!(report.registry, "passed");
        let paths_ok = report.path_discovery == "passed";
        assert_eq!(report.install_ready, paths_ok);
        assert_eq!(
            report.reason_code,
            if paths_ok {
                "SELF_TEST_PASSED"
            } else {
                "USER_ROOT_UNAVAILABLE"
            }
        );
    }
}
