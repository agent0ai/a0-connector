use std::collections::BTreeSet;

use crate::json;
use crate::platform::{architecture, discover_user_paths, Platform};
use crate::registry::BrowserId;
use crate::release::VerifiedReleaseEvidence;
use crate::{
    COMPANION_VERSION, EXIT_INTEGRITY_OR_POLICY, EXIT_RELEASE_UNAVAILABLE, INSTALL_CONTRACT,
    INSTALL_PLAN_CONTRACT,
};

#[path = "install_transaction.rs"]
mod transaction;

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) use transaction::acquire_and_install;
pub(crate) use transaction::lifecycle;
pub(crate) use transaction::verify_installed_status;

pub use transaction::{
    install_verified_candidate, FullyVerifiedInstallCandidate, InstallSource,
    InstallTransactionError, InstallTransactionResult,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InstallOperation {
    Install,
    Update,
}

impl InstallOperation {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Install => "install",
            Self::Update => "update",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum BrowserTarget {
    Auto,
    Explicit(BrowserId),
}

impl BrowserTarget {
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Explicit(browser) => browser.as_str(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct InstallPlan {
    pub operation: InstallOperation,
    pub state: &'static str,
    pub reason_code: &'static str,
    pub mutation_allowed: bool,
    pub catalog: &'static str,
    pub artifact: &'static str,
    pub platform_signature: &'static str,
    pub platform: &'static str,
    pub architecture: &'static str,
    pub install_root: &'static str,
    pub target_browsers: Vec<BrowserTarget>,
    pub registration_count: usize,
    pub rollback: &'static str,
    pub exit_code: u8,
}

impl InstallPlan {
    pub fn to_json(&self) -> String {
        json::object(&[
            ("contract", json::quote(INSTALL_PLAN_CONTRACT)),
            ("schema_version", "1".to_owned()),
            ("companion_version", json::quote(COMPANION_VERSION)),
            ("install_contract", json::quote(INSTALL_CONTRACT)),
            ("operation", json::quote(self.operation.as_str())),
            ("state", json::quote(self.state)),
            ("reason_code", json::quote(self.reason_code)),
            ("mutation_allowed", self.mutation_allowed.to_string()),
            ("catalog", json::quote(self.catalog)),
            ("artifact", json::quote(self.artifact)),
            ("platform_signature", json::quote(self.platform_signature)),
            ("platform", json::quote(self.platform)),
            ("architecture", json::quote(self.architecture)),
            ("install_root", json::quote(self.install_root)),
            (
                "target_browsers",
                json::string_array(self.target_browsers.iter().map(BrowserTarget::as_str)),
            ),
            ("registration_count", self.registration_count.to_string()),
            ("rollback", json::quote(self.rollback)),
        ])
    }
}

pub fn parse_browser_targets(values: &[String]) -> Result<Vec<BrowserTarget>, &'static str> {
    if values.is_empty() {
        return Ok(vec![BrowserTarget::Auto]);
    }
    let mut explicit = BTreeSet::new();
    let mut automatic = false;
    for value in values {
        if value == "auto" {
            automatic = true;
        } else if let Some(browser) = BrowserId::parse(value) {
            explicit.insert(browser);
        } else {
            return Err("UNSUPPORTED_BROWSER");
        }
    }
    if automatic && !explicit.is_empty() {
        return Err("AUTO_BROWSER_MUST_BE_EXCLUSIVE");
    }
    if automatic {
        Ok(vec![BrowserTarget::Auto])
    } else {
        Ok(explicit.into_iter().map(BrowserTarget::Explicit).collect())
    }
}

pub fn plan_install_or_update(
    operation: InstallOperation,
    target_browsers: Vec<BrowserTarget>,
    evidence: Option<&VerifiedReleaseEvidence>,
) -> InstallPlan {
    let platform = Platform::current().as_str();
    let architecture = architecture();
    let install_root = if discover_user_paths().is_ok() {
        "resolved"
    } else {
        "unavailable"
    };
    let Some(evidence) = evidence else {
        return InstallPlan {
            operation,
            state: "blocked",
            reason_code: "RELEASE_EVIDENCE_UNAVAILABLE",
            mutation_allowed: false,
            catalog: "not_verified",
            artifact: "not_verified",
            platform_signature: "not_verified",
            platform,
            architecture,
            install_root,
            target_browsers,
            registration_count: 0,
            rollback: "not_started",
            exit_code: EXIT_RELEASE_UNAVAILABLE,
        };
    };

    if !evidence.is_fully_verified() {
        return InstallPlan {
            operation,
            state: "blocked",
            reason_code: "RELEASE_EVIDENCE_REJECTED",
            mutation_allowed: false,
            catalog: "not_verified",
            artifact: "not_verified",
            platform_signature: "not_verified",
            platform,
            architecture,
            install_root,
            target_browsers,
            registration_count: 0,
            rollback: "not_started",
            exit_code: EXIT_INTEGRITY_OR_POLICY,
        };
    }

    // This legacy evidence object does not bind provenance, offline self-test,
    // or the exact open payload handle consumed by the transaction engine.
    // Only FullyVerifiedInstallCandidate can authorize the real installer.
    InstallPlan {
        operation,
        state: "blocked",
        reason_code: "VERIFIED_INSTALL_CANDIDATE_REQUIRED",
        mutation_allowed: false,
        catalog: "verified",
        artifact: "verified",
        platform_signature: "verified",
        platform,
        architecture,
        install_root,
        target_browsers,
        registration_count: 0,
        rollback: "not_started",
        exit_code: EXIT_INTEGRITY_OR_POLICY,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_release_evidence_cannot_plan_mutations() {
        let plan = plan_install_or_update(
            InstallOperation::Install,
            vec![BrowserTarget::Explicit(BrowserId::Chrome)],
            None,
        );
        assert_eq!(plan.state, "blocked");
        assert!(!plan.mutation_allowed);
        assert_eq!(plan.registration_count, 0);
        assert_eq!(plan.exit_code, EXIT_RELEASE_UNAVAILABLE);
    }

    #[test]
    fn automatic_and_explicit_targets_cannot_mix() {
        assert_eq!(
            parse_browser_targets(&["auto".into(), "chrome".into()]),
            Err("AUTO_BROWSER_MUST_BE_EXCLUSIVE")
        );
    }
}
