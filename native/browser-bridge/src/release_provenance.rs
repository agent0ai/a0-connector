//! Detached signed local-build provenance, independent of GitHub Actions.

use ed25519_dalek::{Signature, VerifyingKey};

use super::{CompositionError, ExpectedRelease};
use crate::json::{self, Value};
use crate::release::{TrustedBuildProvenanceRoot, TRUSTED_BUILD_PROVENANCE_ROOTS};

/// A closed proof bound to the exact archive, executable, catalog and target.
#[derive(Clone)]
pub(crate) struct VerifiedBuildProvenance {
    binding: String,
    signed_bytes: Vec<u8>,
    signature: Vec<u8>,
}

impl VerifiedBuildProvenance {
    pub(super) fn signed_evidence(&self) -> (&[u8], &[u8]) {
        (&self.signed_bytes, &self.signature)
    }
    pub(super) fn matches(&self, expected: &ExpectedRelease<'_>) -> bool {
        self.binding == binding(expected)
    }
}

pub(super) fn verify_local_build_provenance(
    bytes: &[u8],
    signature: &[u8],
    expected: &ExpectedRelease<'_>,
) -> Result<VerifiedBuildProvenance, CompositionError> {
    verify_with_roots(bytes, signature, expected, TRUSTED_BUILD_PROVENANCE_ROOTS)
}

fn binding(expected: &ExpectedRelease<'_>) -> String {
    // Length-delimited JSON avoids separators inside a version/key identifier.
    json::object(&[
        ("release", json::quote(expected.version)),
        ("catalog_key_id", json::quote(expected.catalog_key_id)),
        ("catalog_sha256", json::quote(expected.catalog_sha256)),
        ("archive_sha256", json::quote(expected.archive_sha256)),
        ("executable_sha256", json::quote(expected.executable_sha256)),
        ("executable_size", expected.executable_size.to_string()),
        ("platform", json::quote(expected.platform.as_str())),
        ("artifact_arch", json::quote(expected.artifact_arch)),
    ])
}

fn verify_with_roots(
    bytes: &[u8],
    signature: &[u8],
    expected: &ExpectedRelease<'_>,
    roots: &[TrustedBuildProvenanceRoot],
) -> Result<VerifiedBuildProvenance, CompositionError> {
    let rejected = CompositionError::ProvenanceRejected;
    if roots.is_empty() || bytes.len() > 16 * 1024 || !bytes.is_ascii() || signature.len() != 64 {
        return Err(rejected);
    }
    let value = json::parse(bytes).map_err(|_| rejected)?;
    if value.encode().as_bytes() != bytes {
        return Err(rejected);
    }
    let object = value.as_object().ok_or(rejected)?;
    let keys = [
        "contract",
        "schema_version",
        "release",
        "platform",
        "artifact_arch",
        "catalog_key_id",
        "catalog_sha256",
        "archive_sha256",
        "executable_sha256",
        "executable_size",
        "source_repository",
        "source_commit",
        "source_tree_sha256",
        "rust_toolchain",
        "recipe_sha256",
        "builder_id",
        "signing_key_id",
    ];
    if object.len() != keys.len() || keys.iter().any(|key| !object.contains_key(*key)) {
        return Err(rejected);
    }
    let text = |key: &str| object.get(key).and_then(Value::as_str).ok_or(rejected);
    let matching: Vec<_> = roots
        .iter()
        .filter(|root| Some(root.key_id) == text("signing_key_id").ok())
        .collect();
    if matching.len() != 1 {
        return Err(rejected);
    }
    let root = matching[0];
    let sha = |text: &str| {
        text.len() == 64
            && text
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    };
    let source_commit = text("source_commit")?;
    if text("contract")? != "a0.browser-bridge.local-build-provenance.v1"
        || object.get("schema_version").and_then(Value::as_u64) != Some(1)
        || text("release")? != expected.version
        || text("platform")? != expected.platform.as_str()
        || text("artifact_arch")? != expected.artifact_arch
        || text("catalog_key_id")? != expected.catalog_key_id
        || text("catalog_sha256")? != expected.catalog_sha256
        || text("archive_sha256")? != expected.archive_sha256
        || text("executable_sha256")? != expected.executable_sha256
        || object.get("executable_size").and_then(Value::as_u64) != Some(expected.executable_size)
        || text("builder_id")? != root.builder_id
        || root.builder_id.is_empty()
        || text("source_repository")? != root.source_repository
        || !root.source_repository.starts_with("https://")
        || text("rust_toolchain")? != root.rust_toolchain
        || root.rust_toolchain.is_empty()
        || text("recipe_sha256")? != root.recipe_sha256
        || !sha(root.recipe_sha256)
        || !sha(text("source_tree_sha256")?)
        || ![40, 64].contains(&source_commit.len())
        || !source_commit
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || root.ed25519_public_key.iter().all(|byte| *byte == 0)
    {
        return Err(rejected);
    }
    let key = VerifyingKey::from_bytes(&root.ed25519_public_key).map_err(|_| rejected)?;
    let signature = Signature::from_slice(signature).map_err(|_| rejected)?;
    key.verify_strict(bytes, &signature).map_err(|_| rejected)?;
    Ok(VerifiedBuildProvenance {
        binding: binding(expected),
        signed_bytes: bytes.to_vec(),
        signature: signature.to_bytes().to_vec(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::platform::Platform;
    use ed25519_dalek::{Signer, SigningKey};

    #[test]
    fn detached_receipts_bind_final_bytes_without_native_self_reference() {
        let signing = SigningKey::from_bytes(&[8; 32]);
        let roots = [TrustedBuildProvenanceRoot {
            key_id: "fixture-build-key",
            ed25519_public_key: signing.verifying_key().to_bytes(),
            builder_id: "fixture-local-builder",
            source_repository: "https://source.example.invalid/repository",
            recipe_sha256: "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            rust_toolchain: "1.85.1",
        }];
        let expected = ExpectedRelease {
            version: "2.12.0",
            catalog_key_id: "fixture-catalog-key",
            catalog_sha256: &"a".repeat(64),
            archive_sha256: &"b".repeat(64),
            executable_sha256: &"c".repeat(64),
            executable_size: 100,
            platform: Platform::Macos,
            artifact_arch: "universal",
        };
        let mut fields = json::parse(binding(&expected).as_bytes())
            .unwrap()
            .as_object()
            .unwrap()
            .clone();
        for (key, value) in [
            ("contract", "a0.browser-bridge.local-build-provenance.v1"),
            ("source_repository", roots[0].source_repository),
            ("source_commit", &"d".repeat(40)),
            ("source_tree_sha256", &"f".repeat(64)),
            ("rust_toolchain", roots[0].rust_toolchain),
            ("recipe_sha256", roots[0].recipe_sha256),
            ("builder_id", roots[0].builder_id),
            ("signing_key_id", roots[0].key_id),
        ] {
            fields.insert(key.to_owned(), Value::String(value.to_owned()));
        }
        fields.insert("schema_version".into(), Value::Number("1".into()));
        let bytes = Value::Object(fields.clone()).encode().into_bytes();
        let signature = signing.sign(&bytes).to_bytes();
        assert!(verify_with_roots(&bytes, &signature, &expected, &roots)
            .unwrap()
            .matches(&expected));
        assert!(verify_local_build_provenance(&bytes, &signature, &expected).is_err());
        assert!(verify_with_roots(&bytes, &[0; 64], &expected, &roots).is_err());
        for field in [
            "archive_sha256",
            "executable_sha256",
            "recipe_sha256",
            "source_repository",
            "builder_id",
        ] {
            let mut changed = fields.clone();
            changed.insert(field.into(), Value::String("unapproved".into()));
            let bytes = Value::Object(changed).encode().into_bytes();
            // Even a correctly signed statement cannot widen the builder's policy.
            assert!(
                verify_with_roots(&bytes, &signing.sign(&bytes).to_bytes(), &expected, &roots)
                    .is_err()
            );
        }
        // Publish a different final executable/catalog after compilation. The
        // same precompiled signer policy verifies its detached receipt: no
        // final digest needs embedding in the executable being authenticated.
        let second = ExpectedRelease {
            catalog_sha256: &"1".repeat(64),
            executable_sha256: &"2".repeat(64),
            ..expected
        };
        fields.insert(
            "catalog_sha256".into(),
            Value::String(second.catalog_sha256.into()),
        );
        fields.insert(
            "executable_sha256".into(),
            Value::String(second.executable_sha256.into()),
        );
        let second_bytes = Value::Object(fields).encode().into_bytes();
        let second_signature = signing.sign(&second_bytes).to_bytes();
        let receipt = verify_with_roots(&second_bytes, &second_signature, &second, &roots).unwrap();
        assert!(receipt.matches(&second));
        assert!(!receipt.matches(&expected));
        assert!(verify_with_roots(&second_bytes, &second_signature, &expected, &roots).is_err());
        assert_eq!(
            receipt.signed_evidence(),
            (second_bytes.as_slice(), second_signature.as_slice())
        );
    }
}
