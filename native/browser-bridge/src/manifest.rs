use std::collections::BTreeSet;
use std::path::Path;

use crate::json;
use crate::release::VerifiedReleaseEvidence;
use crate::NATIVE_HOST_NAME;

#[cfg(not(feature = "local-development"))]
const DESCRIPTION: &str = "Agent Zero browser bridge";
#[cfg(feature = "local-development")]
const DESCRIPTION: &str = "Agent Zero browser bridge (Development)";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ManifestError {
    BinaryPathNotAbsolute,
    BinaryPathNotUnicode,
    NoVerifiedOrigins,
    InvalidOrigin,
    DuplicateOrigin,
    ReleaseEvidenceNotVerified,
}

impl ManifestError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::BinaryPathNotAbsolute => "BINARY_PATH_NOT_ABSOLUTE",
            Self::BinaryPathNotUnicode => "BINARY_PATH_NOT_UNICODE",
            Self::NoVerifiedOrigins => "VERIFIED_EXTENSION_ORIGINS_REQUIRED",
            Self::InvalidOrigin => "INVALID_EXTENSION_ORIGIN",
            Self::DuplicateOrigin => "DUPLICATE_EXTENSION_ORIGIN",
            Self::ReleaseEvidenceNotVerified => "VERIFIED_RELEASE_REQUIRED",
        }
    }
}

pub fn generate_production_manifest(
    binary_path: &Path,
    release: &VerifiedReleaseEvidence,
) -> Result<String, ManifestError> {
    if !release.is_fully_verified() {
        return Err(ManifestError::ReleaseEvidenceNotVerified);
    }
    generate_exact_manifest(binary_path, crate::release::PRODUCTION_EXTENSION_ORIGINS)
}

#[cfg(feature = "local-development")]
pub(crate) fn generate_development_manifest(binary_path: &Path) -> Result<String, ManifestError> {
    generate_exact_manifest(binary_path, &[crate::DEVELOPMENT_EXTENSION_ORIGIN])
}

pub(crate) fn generate_exact_manifest<S: AsRef<str>>(
    binary_path: &Path,
    allowed_origins: &[S],
) -> Result<String, ManifestError> {
    if !binary_path.is_absolute() {
        return Err(ManifestError::BinaryPathNotAbsolute);
    }
    let Some(binary_path) = binary_path.to_str() else {
        return Err(ManifestError::BinaryPathNotUnicode);
    };
    if allowed_origins.is_empty() {
        return Err(ManifestError::NoVerifiedOrigins);
    }

    let mut seen = BTreeSet::new();
    for origin in allowed_origins {
        let origin = origin.as_ref();
        if !is_exact_extension_origin(origin) {
            return Err(ManifestError::InvalidOrigin);
        }
        if !seen.insert(origin) {
            return Err(ManifestError::DuplicateOrigin);
        }
    }

    let origins = allowed_origins
        .iter()
        .map(|origin| format!("    {}", json::quote(origin.as_ref())))
        .collect::<Vec<_>>()
        .join(",\n");
    Ok(format!(
        "{{\n  \"name\": {},\n  \"description\": {},\n  \"path\": {},\n  \"type\": \"stdio\",\n  \"allowed_origins\": [\n{}\n  ]\n}}\n",
        json::quote(NATIVE_HOST_NAME),
        json::quote(DESCRIPTION),
        json::quote(binary_path),
        origins,
    ))
}

pub fn is_exact_extension_origin(origin: &str) -> bool {
    crate::release::is_exact_release_extension_origin(origin)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE_ORIGIN: &str = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/";

    #[test]
    fn exact_manifest_is_deterministic() {
        let manifest = generate_exact_manifest(
            Path::new("/opt/agent-zero/releases/0.0.0-test/a0-browser-bridge"),
            &[FIXTURE_ORIGIN],
        )
        .expect("fixture manifest should generate");
        let expected = format!(
            concat!(
                "{{\n",
                "  \"name\": {},\n",
                "  \"description\": {},\n",
                "  \"path\": \"/opt/agent-zero/releases/0.0.0-test/a0-browser-bridge\",\n",
                "  \"type\": \"stdio\",\n",
                "  \"allowed_origins\": [\n",
                "    \"chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/\"\n",
                "  ]\n",
                "}}\n",
            ),
            json::quote(NATIVE_HOST_NAME),
            json::quote(DESCRIPTION),
        );
        assert_eq!(manifest, expected);
    }

    #[test]
    fn wildcard_and_relative_inputs_are_rejected() {
        assert!(!is_exact_extension_origin("chrome-extension://*/"));
        assert_eq!(
            generate_exact_manifest(Path::new("relative/a0-browser-bridge"), &[FIXTURE_ORIGIN]),
            Err(ManifestError::BinaryPathNotAbsolute),
        );
    }
}
