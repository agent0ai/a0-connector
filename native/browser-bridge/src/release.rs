//! Compiled release trust boundary.
//!
//! The owner-provided CWS draft origin and verified macOS signing identity are
//! provisioned public identities, not store approval or release authority.
//! The catalog verifier is separate from platform and install-chain admission.
//! Genuine independent publisher/builder public roots enable verification, not
//! artifact approval. Missing or invalid signed downloads, platform evidence,
//! installed proof and Core runtime authority continue to fail closed.

use std::collections::BTreeSet;

#[path = "release_catalog.rs"]
pub mod catalog;

pub const RELEASE_POLICY_CONTRACT: &str = "a0.browser-bridge.release-policy.v1";
pub const RELEASE_POLICY_SOURCE: &str = include_str!("../release-policy-v1.json");

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TrustedReleasePublicKey {
    pub key_id: &'static str,
    pub ed25519_public_key: [u8; 32],
}

/// True only after the production build contains and invokes the reviewed
/// detached-signature verifier. A key identifier is not proof of readiness.
pub const RELEASE_SIGNATURE_VERIFIER_READY: bool = true;
pub const TRUSTED_RELEASE_PUBLIC_KEYS: &[TrustedReleasePublicKey] = &[TrustedReleasePublicKey {
    key_id: "publisher-2026",
    ed25519_public_key: [
        0x18, 0x43, 0xb2, 0x80, 0xfd, 0x2b, 0x05, 0x89, 0x55, 0x61, 0x91, 0x31, 0xf9, 0xb8, 0x03,
        0x85, 0x46, 0x77, 0xb1, 0x5a, 0x5f, 0x57, 0x27, 0x9d, 0xa0, 0x8f, 0x49, 0xa3, 0xe6, 0xdc,
        0x60, 0x82,
    ],
}];
/// CWS draft identity only; native invocation still grants no runtime authority.
pub const PRODUCTION_EXTENSION_ORIGINS: &[&str] =
    &["chrome-extension://nhliclifilepdkoolioacpjpijomfplj/"];

/// Reviewed immutable catalog locations, never supplied by a page, paired
/// server, environment variable, or an unsigned update response.
pub struct PinnedReleaseCatalog {
    pub release: &'static str,
    pub catalog_url: &'static str,
    pub signature_url: &'static str,
}

pub const PINNED_RELEASE_CATALOGS: &[PinnedReleaseCatalog] = &[PinnedReleaseCatalog {
    release: "2.12.3",
    catalog_url: "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/native-v2.12.3-macos/catalog.json",
    signature_url: "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/native-v2.12.3-macos/catalog.sig",
}];

/// Immutable detached derivation receipts, signed after the payload/catalog.
/// Neither final catalog nor executable hashes can be embedded in that same
/// executable: doing so would require an impossible cryptographic fixed point.
/// Independently distributed CLI/bootstrap packages may pin those final hashes.
pub struct PinnedBuildProvenance {
    pub release: &'static str,
    pub platform: &'static str,
    pub artifact_arch: &'static str,
    pub statement_url: &'static str,
    pub signature_url: &'static str,
}

pub const PINNED_BUILD_PROVENANCE: &[PinnedBuildProvenance] = &[PinnedBuildProvenance {
    release: "2.12.3",
    platform: "macos",
    artifact_arch: "universal2",
    statement_url: "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/native-v2.12.3-macos/provenance-macos-universal2.json",
    signature_url: "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/native-v2.12.3-macos/provenance-macos-universal2.sig",
}];

/// Optional independently provisioned local-release provenance signers. Catalog
/// roots do not implicitly authorize a builder, and a statement cannot add keys.
pub struct TrustedBuildProvenanceRoot {
    pub key_id: &'static str,
    pub ed25519_public_key: [u8; 32],
    pub builder_id: &'static str,
    pub source_repository: &'static str,
    pub recipe_sha256: &'static str,
    pub rust_toolchain: &'static str,
}

pub const TRUSTED_BUILD_PROVENANCE_ROOTS: &[TrustedBuildProvenanceRoot] =
    &[TrustedBuildProvenanceRoot {
        key_id: "builder-2026",
        ed25519_public_key: [
            0x6e, 0x05, 0x68, 0x94, 0x8a, 0xcd, 0x3d, 0x3e, 0xe2, 0x8e, 0x85, 0x19, 0x1f, 0x10,
            0x46, 0x60, 0x40, 0xdf, 0x13, 0x97, 0x80, 0xe8, 0xce, 0x7e, 0x54, 0xea, 0x23, 0x52,
            0x30, 0xc8, 0xb7, 0x58,
        ],
        builder_id: "agent-zero-local-macos-2026",
        source_repository: "https://github.com/TerminallyLazy/a0-connector",
        recipe_sha256: "f94eca3a0b05c7ea8fe3965c02bd6b31922c066535a2e30b8d2519d34e550dc7",
        rust_toolchain: "rustc 1.85.1 (4eb161250 2025-03-15)",
    }];

/// Exact publisher identity for an approved Developer ID Application release.
/// Apple Development and Apple Distribution identities are not substitutes.
pub struct MacosReleaseIdentity {
    pub team_id: &'static str,
    pub signing_identifier: &'static str,
}

pub const MACOS_RELEASE_IDENTITY: Option<MacosReleaseIdentity> = Some(MacosReleaseIdentity {
    team_id: "R2KNNFH5FC",
    signing_identifier: "io.agentzero.browser_bridge",
});

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReleaseEvidenceState {
    Unavailable,
    Rejected,
    Verified,
}

impl ReleaseEvidenceState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Unavailable => "not_verified",
            Self::Rejected => "not_verified",
            Self::Verified => "verified",
        }
    }
}

#[derive(Clone, Debug)]
pub struct VerifiedReleaseEvidence {
    catalog: ReleaseEvidenceState,
    artifact: ReleaseEvidenceState,
    platform_signature: ReleaseEvidenceState,
    catalog_signature_verified: bool,
    release_key_id: String,
    extension_origins: Vec<String>,
}

impl VerifiedReleaseEvidence {
    pub fn is_fully_verified(&self) -> bool {
        let supplied = self
            .extension_origins
            .iter()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        let compiled = PRODUCTION_EXTENSION_ORIGINS
            .iter()
            .copied()
            .collect::<BTreeSet<_>>();
        self.catalog == ReleaseEvidenceState::Verified
            && self.artifact == ReleaseEvidenceState::Verified
            && self.platform_signature == ReleaseEvidenceState::Verified
            && self.catalog_signature_verified
            && release_trust_configured()
            && trusted_release_key(&self.release_key_id).is_some()
            && !self.extension_origins.is_empty()
            && supplied.len() == self.extension_origins.len()
            && supplied == compiled
    }

    #[cfg(test)]
    pub(crate) fn untrusted_fixture(origins: Vec<String>) -> Self {
        Self {
            catalog: ReleaseEvidenceState::Verified,
            artifact: ReleaseEvidenceState::Verified,
            platform_signature: ReleaseEvidenceState::Verified,
            catalog_signature_verified: true,
            release_key_id: "fixture-key".to_owned(),
            extension_origins: origins,
        }
    }
}

fn trusted_release_key(key_id: &str) -> Option<&'static TrustedReleasePublicKey> {
    TRUSTED_RELEASE_PUBLIC_KEYS.iter().find(|key| {
        key.key_id == key_id
            && !key.key_id.is_empty()
            && key.ed25519_public_key.iter().any(|byte| *byte != 0)
    })
}

pub fn release_trust_configured() -> bool {
    let key_ids = TRUSTED_RELEASE_PUBLIC_KEYS
        .iter()
        .map(|key| key.key_id)
        .collect::<BTreeSet<_>>();
    let origins = PRODUCTION_EXTENSION_ORIGINS
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    RELEASE_SIGNATURE_VERIFIER_READY
        && !TRUSTED_RELEASE_PUBLIC_KEYS.is_empty()
        && key_ids.len() == TRUSTED_RELEASE_PUBLIC_KEYS.len()
        && TRUSTED_RELEASE_PUBLIC_KEYS.iter().all(|key| {
            !key.key_id.is_empty() && key.ed25519_public_key.iter().any(|byte| *byte != 0)
        })
        && !PRODUCTION_EXTENSION_ORIGINS.is_empty()
        && origins.len() == PRODUCTION_EXTENSION_ORIGINS.len()
        && PRODUCTION_EXTENSION_ORIGINS
            .iter()
            .all(|origin| is_exact_release_extension_origin(origin))
}

pub(crate) fn is_exact_release_extension_origin(origin: &str) -> bool {
    const PREFIX: &str = "chrome-extension://";
    let Some(extension_id) = origin
        .strip_prefix(PREFIX)
        .and_then(|value| value.strip_suffix('/'))
    else {
        return false;
    };
    extension_id.len() == 32
        && extension_id.bytes().all(|byte| matches!(byte, b'a'..=b'p'))
        && !origin.contains('*')
}

pub fn verified_release_evidence_unavailable() -> Option<VerifiedReleaseEvidence> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provisioned_release_roots_cannot_authorize_a_fixture() {
        let evidence = VerifiedReleaseEvidence::untrusted_fixture(vec![
            "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/".to_owned(),
        ]);
        assert!(release_trust_configured());
        assert!(!evidence.is_fully_verified());
    }

    #[test]
    fn provisioned_public_identities_do_not_mint_install_evidence() {
        assert_eq!(
            PRODUCTION_EXTENSION_ORIGINS,
            &["chrome-extension://nhliclifilepdkoolioacpjpijomfplj/"]
        );
        let identity = MACOS_RELEASE_IDENTITY.as_ref().unwrap();
        assert_eq!(identity.team_id, "R2KNNFH5FC");
        assert_eq!(identity.signing_identifier, "io.agentzero.browser_bridge");
        assert_eq!(TRUSTED_RELEASE_PUBLIC_KEYS.len(), 1);
        assert_eq!(TRUSTED_BUILD_PROVENANCE_ROOTS.len(), 1);
        assert_ne!(
            TRUSTED_RELEASE_PUBLIC_KEYS[0].ed25519_public_key,
            TRUSTED_BUILD_PROVENANCE_ROOTS[0].ed25519_public_key
        );
        assert_eq!(PINNED_RELEASE_CATALOGS.len(), 1);
        assert_eq!(PINNED_BUILD_PROVENANCE.len(), 1);
        assert_eq!(PINNED_BUILD_PROVENANCE[0].platform, "macos");
        assert_eq!(PINNED_BUILD_PROVENANCE[0].artifact_arch, "universal2");
        assert!(RELEASE_SIGNATURE_VERIFIER_READY);
        assert!(release_trust_configured());
        assert!(verified_release_evidence_unavailable().is_none());
        let evidence = VerifiedReleaseEvidence::untrusted_fixture(
            PRODUCTION_EXTENSION_ORIGINS
                .iter()
                .map(|origin| (*origin).to_owned())
                .collect(),
        );
        assert!(!evidence.is_fully_verified());
        assert!(crate::manifest::generate_production_manifest(
            std::path::Path::new("/candidate/a0-browser-bridge"),
            &evidence
        )
        .is_err());
        #[cfg(not(feature = "local-development"))]
        assert!(crate::native_host::validate_invocation(&[
            PRODUCTION_EXTENSION_ORIGINS[0].to_owned()
        ])
        .is_ok());
    }

    #[test]
    fn key_identifiers_cannot_substitute_for_pinned_key_bytes_or_a_verifier() {
        assert!(trusted_release_key("fixture-key").is_none());
        assert!(trusted_release_key("builder-2026").is_none());
        assert!(trusted_release_key("publisher-2026").is_some());
        // Exercise the production entry point, not an injected test trust root.
        // The shape reaches verify_strict before artifact coverage validation.
        let bytes = br#"{"artifacts":[],"channel":"stable","extension_origins":["chrome-extension://nhliclifilepdkoolioacpjpijomfplj/"],"minimum_secure_companion":"2.12.0","platforms":["macos"],"protocol":{"max":1,"min":1},"published_at":"2026-09-05T00:00:00Z","release":"2.12.0","release_key_id":"publisher-2026","schema_version":2,"trust":{"max":1,"min":1}}"#;
        assert_eq!(
            catalog::verify_catalog(bytes, &[0; 64], "2.12.0").unwrap_err(),
            catalog::CatalogError::InvalidSignature
        );
    }
}
